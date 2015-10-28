'''
Module of Stochastic Feed Forward Networks
'''

from collections import OrderedDict
import numpy as np
import theano
from theano import tensor as T
from theano.sandbox.rng_mrg import MRG_RandomStreams as RandomStreams

from layers import Layer
from layers import MLP
import tools
from tools import init_rngs
from tools import init_weights
from tools import log_mean_exp
from tools import logit
from tools import _slice


norm_weight = tools.norm_weight
ortho_weight = tools.ortho_weight
floatX = 'float32' # theano.config.floatX


def init_momentum_args(model, momentum=0.9, **kwargs):
    model.momentum = momentum
    return kwargs

def init_sgd_args(model, **kwargs):
    return kwargs

def init_inference_args(model,
                        inference_rate=0.1,
                        inference_decay=0.99,
                        entropy_scale=1.0,
                        importance_sampling=False,
                        n_inference_samples=20,
                        inference_scaling=None,
                        inference_method='momentum',
                        sample_from_joint=False,
                        alpha = 7,
                        **kwargs):
    model.inference_rate = inference_rate
    model.inference_decay = inference_decay
    model.entropy_scale = entropy_scale
    model.importance_sampling = importance_sampling
    model.inference_scaling = inference_scaling
    model.sample_from_joint = sample_from_joint
    model.n_inference_samples = n_inference_samples
    model.alpha = alpha

    if inference_method == 'sgd':
        model.step_infer = model._step_sgd
        model.init_infer = model._init_sgd
        model.unpack_infer = model._unpack_sgd
        model.params_infer = model._params_sgd
        kwargs = init_sgd_args(model, **kwargs)
    elif inference_method == 'momentum':
        model.step_infer = model._step_momentum
        model.init_infer = model._init_momentum
        model.unpack_infer = model._unpack_momentum
        model.params_infer = model._params_momentum
        kwargs = init_momentum_args(model, **kwargs)
    else:
        raise ValueError()

    return kwargs


class SigmoidBeliefNetwork(Layer):
    def __init__(self, dim_in, dim_h, dim_out,
                 posterior=None, conditional=None,
                 z_init=None,
                 x_noise_mode=None, y_noise_mode=None, noise_amount=0.1,
                 name='sbn',
                 **kwargs):

        self.dim_in = dim_in
        self.dim_h = dim_h
        self.dim_out = dim_out

        self.posterior = posterior
        self.conditional = conditional

        self.z_init = z_init

        self.x_mode = x_noise_mode
        self.y_mode = y_noise_mode
        self.noise_amount = noise_amount

        kwargs = init_inference_args(self, **kwargs)
        kwargs = init_weights(self, **kwargs)
        kwargs = init_rngs(self, **kwargs)

        super(SigmoidBeliefNetwork, self).__init__(name=name)

    def set_params(self):
        z = np.zeros((self.dim_h,)).astype(floatX)
        inference_scale_factor = np.float32(1.0)

        self.params = OrderedDict(
            z=z, inference_scale_factor=inference_scale_factor)

        if self.posterior is None:
            self.posterior = MLP(self.dim_in, self.dim_h, self.dim_h, 1,
                                 rng=self.rng, trng=self.trng,
                                 h_act='T.nnet.sigmoid',
                                 out_act='T.nnet.sigmoid')
        if self.conditional is None:
            self.conditional = MLP(self.dim_h, self.dim_out, self.dim_out, 1,
                                   rng=self.rng, trng=self.trng,
                                   h_act='T.nnet.sigmoid',
                                   out_act='T.nnet.sigmoid')

        self.posterior.name = self.name + '_posterior'
        self.conditional.name = self.name + '_conditional'

    def set_tparams(self, excludes=[]):
        excludes.append('inference_scale_factor')
        excludes = ['{name}_{key}'.format(name=self.name, key=key)
                    for key in excludes]
        tparams = super(SigmoidBeliefNetwork, self).set_tparams()
        tparams.update(**self.posterior.set_tparams())
        tparams.update(**self.conditional.set_tparams())
        tparams = OrderedDict((k, v) for k, v in tparams.iteritems()
            if k not in excludes)

        return tparams

    def _sample(self, p, size=None):
        if size is None:
            size = p.shape
        return self.trng.binomial(p=p, size=size, n=1, dtype=p.dtype)

    def _noise(self, x, amount, size):
        return x * (1 - self.trng.binomial(p=amount,
                                           size=size, n=1, dtype=x.dtype))

    def set_input(self, x, mode, size=None):
        if size is None:
            size = x.shape
        if mode == 'sample':
            x = self._sample(x[None, :, :], size=size)
        elif mode == 'noise':
            x = self._sample(x)
            x = self._noise(x[None, :, :], size=size)
        elif mode is None:
            x = self._sample(x, size=x.shape)
            x = T.alloc(0., *size) + x[None, :, :]
        else:
            raise ValueError('% not supported' % mode)
        return x

    def init_inputs(self, x, y, steps=1):
        x_size = (steps, x.shape[0], x.shape[1])
        y_size = (steps, y.shape[0], y.shape[1])

        x = self.set_input(x, self.x_mode, size=x_size)
        y = x.copy()
        return x, y

    def get_params(self):
        params = [self.z] + self.conditional.get_params() + self.posterior.get_params() + [self.inference_scale_factor]
        return params

    def p_y_given_h(self, h, *params):
        params = params[1:1+len(self.conditional.get_params())]
        return self.conditional.step_call(h, *params)

    def sample_from_prior(self, n_samples=100):
        p = T.nnet.sigmoid(self.z)
        h = self.posterior.sample(p=p, size=(n_samples, self.dim_h))
        py = self.conditional(h)
        return py

    def m_step(self, ph, y, z, n_samples=10):
        constants = []
        q = T.nnet.sigmoid(z)
        prior = T.nnet.sigmoid(self.z)

        if n_samples == 0:
            h = q[None, :, :]
        else:
            h = self.posterior.sample(
                q, size=(n_samples, q.shape[0], q.shape[1]))

        py = self.conditional(h)
        py_approx = self.conditional(q)
        y_energy_approx = self.conditional.neg_log_prob(y, py_approx).mean()

        if self.importance_sampling:
            y_energy = self.conditional.neg_log_prob(y[None, :, :], py).mean()
            prior_energy = self.posterior.neg_log_prob(h, prior[None, None, :])
            entropy_term = self.posterior.neg_log_prob(h, q[None, :, :])
            w = T.exp(-y_energy
                      - prior_energy
                      + entropy_term)
            w = T.clip(w, 1e-7, 1)
            w_sum = w.sum(axis=0)
            w_tilda = w / w_sum[None, :]
            y_energy = (w_tilda * y_energy).sum(axis=0).mean()
            prior_energy = (w_tilda * prior_energy).sum(axis=0).mean()
            constants += [w_tilda, w]
        elif self.sample_from_joint:
            raise NotImplementedError()
            y_hat = self.conditional.sample(
                py, size=(n_samples, py.shape[0], py.shape[1]))
            ph = self.posterior(y_hat)
            h_energy = self.posterior.neg_log_prob(h).mean()
        else:
            y_energy = self.conditional.neg_log_prob(y[None, :, :], py).mean()
            prior_energy = self.posterior.neg_log_prob(q, prior[None, :]).mean()

        h_energy = self.posterior.neg_log_prob(q, ph).mean()
        entropy = self.posterior.entropy(q).mean()

        return (prior_energy, h_energy, y_energy, y_energy_approx, entropy), constants

    def kl_divergence(self, p, q, entropy_scale=1.0):
        entropy_term = entropy_scale * self.posterior.entropy(p)
        prior_term = self.posterior.neg_log_prob(p, q)
        return -(entropy_term - prior_term)

    def e_step(self, ph, y, z, *params):
        prior = T.nnet.sigmoid(params[0])
        q = T.nnet.sigmoid(z)
        py = self.p_y_given_h(q, *params)

        consider_constant = [y, prior]
        cond_term = self.conditional.neg_log_prob(y, py)

        if isinstance(self.inference_scaling, float):
            cond_term = self.inference_scaling * cond_term

        elif self.inference_scaling == 'global':
            print 'Using global scaling in inference'
            scale_factor = params[-1]
            cond_term = scale_factor * cond_term
            consider_constant += [scale_factor]

        elif self.inference_scaling == 'inference':
            print 'Calculating scaling during inference'
            h = self.posterior.sample(mu, size=(10, mu.shape[0], mu.shape[1]))
            py_r = self.p_y_given_h(h, *params)
            mc = self.conditional.neg_log_prob(y[None, :, :], py_r).mean(axis=0)
            cond_term_c = T.zeros_like(cond_term) + cond_term
            scale_factor = mc / cond_term_c
            cond_term = scale_factor * cond_term
            consider_constant += [scale_factor, cond_term_c]

        elif self.inference_scaling == 'KL':
            raise NotImplementedError()
            print 'Adding KL term to inference'
            mc = self.conditional.neg_log_prob(y[None, :, :], py_r).mean(axis=0)

        elif self.inference_scaling == 'reweight':
            print 'Reweighting mus'

        elif self.inference_scaling == 'marginal':
            pass
        elif self.inference_scaling == 'stochastic':
            pass
        elif self.inference_scaling == 'conditional_only':
            pass
        elif self.inference_scaling == 'recognition_net':
            pass
        elif self.inference_scaling == 'continuous':
            print 'Approximate continuous Bernoulli'
            u = self.trng.uniform(low=0, high=1, size=(self.n_inference_samples, q.shape[0], q.shape[1])).astype(floatX)
            p = (u + q[None, :, :] - 0.5)
            alpha = self.alpha
            h = p ** alpha / ((1 - p) ** alpha + p ** alpha)
            h = T.clip(h, .0, 1.)
            py = self.p_y_given_h(h, *params)
            cond_term = self.conditional.neg_log_prob(y[None, :, :], py).mean(axis=(0))
        elif self.inference_scaling is not None:
            raise ValueError(self.inference_scaling)
        else:
            print 'No inference scaling'

        if self.inference_scaling == 'conditional_only':
            print 'Conditional-only inference'
            kl_term = 0. * cond_term
            cost = cond_term.sum(axis=0)
        elif self.inference_scaling == 'recognition_net':
            print 'Using recognition as posterior'
            kl_term = self.kl_divergence(
                q, ph, entropy_scale=self.entropy_scale)
            kl_term += self.kl_divergence(q, prior[None, :])
            cost = (cond_term + kl_term).sum(axis=0)
        else:
            kl_term = self.kl_divergence(q, prior[None, :], entropy_scale=self.entropy_scale)
            cost = (cond_term + kl_term).sum(axis=0)

        grad = theano.grad(cost, wrt=z, consider_constant=consider_constant)

        return cost, grad

    def sample_e(self, q, *params):
        prior = T.nnet.sigmoid(params[0])
        h = self.posterior.sample(q, size=(100, q.shape[0], q.shape[1]))

        py = self.p_y_given_h(h, *params)

        cond_term = self.conditional.neg_log_prob(y[None, :, :], py)
        prior_term = self.posterior.neg_log_prob(h, prior[None, None, :])
        posterior_term = self.posterior.neg_log_prob(h, q[None, :, :])

        w = T.exp(-cond_term - prior_term + posterior_term)
        w = T.clip(w, 1e-7, 1.0)
        w_tilda = w / w.sum(axis=0)[None, :]
        q = (w_tilda[:, :, None] * h).sum(axis=0)
        return q

    def step_infer(self, *params):
        raise NotImplementedError()

    def init_infer(self, z):
        raise NotImplementedError()

    def unpack_infer(self, outs):
        raise NotImplementedError()

    def params_infer(self):
        raise NotImplementedError()

    # SGD
    def _step_sgd(self, ph, y, z, l, *params):
        cost, grad = self.e_step(ph, y, z, *params)
        z = (z - l * grad).astype(floatX)
        l *= self.inference_decay
        return z, l, cost

    def _init_sgd(self, ph, y, z):
        return [self.inference_rate]

    def _unpack_sgd(self, outs):
        zs, ls, costs = outs
        return zs, costs

    def _params_sgd(self):
        return []

    # Momentum
    def _step_momentum(self, ph, y, z, l, dz_, m, *params):
        cost, grad = self.e_step(ph, y, z, *params)
        dz = (-l * grad + m * dz_).astype(floatX)
        z_ = (z + dz).astype(floatX)
        if self.inference_scaling == 'reweight':
            q = T.nnet.sigmoid(z_)
            prior = T.nnet.sigmoid(params[0])
            h = self.posterior.sample(q, size=(100, q.shape[0], q.shape[1]))
            py_r = self.p_y_given_h(h, *params)
            cond_term = self.conditional.neg_log_prob(y[None, :, :], py_r)
            prior_term = self.posterior.neg_log_prob(h, prior[None, None, :])
            posterior_term = self.posterior.neg_log_prob(h, q[None, :, :])
            w = T.exp(-cond_term - prior_term + posterior_term)
            w = T.clip(w, 1e-7, 1.0)
            w_tilda = w / w.sum(axis=0)[None, :]
            mu = (w_tilda[:, :, None] * h).sum(axis=0)
            z = logit(q)
        else:
            z = z_
        l *= self.inference_decay
        return z, l, dz, cost

    def _init_momentum(self, ph, y, z):
        return [self.inference_rate, T.zeros_like(z)]

    def _unpack_momentum(self, outs):
        zs, ls, dzs, costs = outs
        return zs, costs

    def _params_momentum(self):
        return [T.constant(self.momentum).astype('float32')]

    def infer_q(self, x, y, n_inference_steps, z0=None):
        updates = theano.OrderedUpdates()

        xs, ys = self.init_inputs(x, y, steps=n_inference_steps)
        ph = self.posterior(xs)
        if z0 is None:
            if self.z_init == 'recognition_net':
                print 'Starting z0 at recognition net'
                z0 = logit(ph[0])
            else:
                z0 = T.alloc(0., x.shape[0], self.dim_h).astype(floatX)

        seqs = [ph, ys]
        outputs_info = [z0] + self.init_infer(ph[0], ys[0], z0) + [None]
        non_seqs = self.params_infer() + self.get_params()

        outs, updates_2 = theano.scan(
            self.step_infer,
            sequences=seqs,
            outputs_info=outputs_info,
            non_sequences=non_seqs,
            name=tools._p(self.name, 'infer'),
            n_steps=n_inference_steps,
            profile=tools.profile,
            strict=True
        )
        updates.update(updates_2)

        zs, i_costs = self.unpack_infer(outs)
        zs = T.concatenate([z0[None, :, :], zs], axis=0)

        return (ph, xs, ys, zs), updates

    # Inference
    def inference(self, x, y, z0=None, n_inference_steps=20, n_samples=100):

        (ph, xs, ys, zs), updates = self.infer_q(
            x, y, n_inference_steps, z0=z0)

        (prior_energy, h_energy, y_energy,
         y_energy_approx, entropy), m_constants = self.m_step(
            ph[0], ys[0], zs[-1], n_samples=n_samples)

        constants = [xs, ys, zs, entropy] + m_constants

        if self.inference_scaling == 'global':
            updates += [
                (self.inference_scale_factor, y_energy / y_energy_approx)]
            constants += [self.inference_scale_factor]

        return (xs, ys, zs,
                prior_energy, h_energy, y_energy,
                y_energy_approx, entropy), updates, constants

    def __call__(self, x, y, ph=None, n_samples=100,
                 n_inference_steps=0, end_with_inference=True):
        outs = OrderedDict()
        updates = theano.OrderedUpdates()
        prior = T.nnet.sigmoid(self.z)

        if end_with_inference:
            if ph is None:
                z0 = None
            else:
                z0 = logit(ph)
            (ph_x, xs, ys, zs), updates_i = self.infer_q(x, y, n_inference_steps, z0=z0)
            updates.update(updates_i)
            q = T.nnet.sigmoid(zs)
        elif ph is None:
            x = self.trng.binomial(p=x, size=x.shape, n=1, dtype=x.dtype)
            q = self.posterior(x)
            ys = x.copy()
        else:
            ys = x.copy()

        if end_with_inference:
            if n_samples == 0:
                h = q[None, :, :, :]
            else:
                h = self.posterior.sample(
                    q, size=(n_samples, q.shape[0], q.shape[1], q.shape[2]))

            pys = self.conditional(h)
            py_approx = self.conditional(q)

            conds_app = self.conditional.neg_log_prob(y[None, :, :], py_approx).mean(axis=1)
            conds_mc = self.conditional.neg_log_prob(y[None, :, :], pys).mean(axis=(0, 2))
            kl_terms = self.kl_divergence(q, prior[None, None, :]).mean(axis=1)

            y_energy = conds_mc[-1]
            kl_term = kl_terms[-1]
            py = pys[-1]

            outs.update(
                c_a=conds_app,
                c_mc=conds_mc,
                kl=kl_terms
            )
        else:
            if n_samples == 0:
                h = q[None, :, :]
            else:
                h = self.posterior.sample(
                    q, size=(n_samples, q.shape[0], q.shape[1]))

            py = self.conditional(h)
            y_energy = self.conditional.neg_log_prob(ys, py).mean(axis=(0, 1))
            kl_term = self.kl_divergence(q, prior[None, :]).mean(axis=0)

        outs.update(
            py=py,
            lower_bound=(y_energy+kl_term)
        )

        return outs, updates


class GaussianBeliefNet(Layer):
    def __init__(self, dim_in, dim_h, dim_out,
                 posterior=None, conditional=None,
                 z_init=None,
                 x_noise_mode=None, y_noise_mode=None, noise_amount=0.1,
                 name='gbn',
                 **kwargs):

        self.dim_in = dim_in
        self.dim_h = dim_h
        self.dim_out = dim_out

        self.posterior = posterior
        self.conditional = conditional

        self.z_init = z_init

        self.x_mode = x_noise_mode
        self.y_mode = y_noise_mode
        self.noise_amount = noise_amount

        kwargs = init_inference_args(self, **kwargs)
        kwargs = init_weights(self, **kwargs)
        kwargs = init_rngs(self, **kwargs)

        super(GaussianBeliefNet, self).__init__(name=name)

    def set_params(self):
        mu = np.zeros((self.dim_h,)).astype(floatX)
        log_sigma = np.zeros((self.dim_h,)).astype(floatX)
        inference_scale_factor = np.float32(1.0)

        self.params = OrderedDict(
            mu=mu, log_sigma=log_sigma,
            inference_scale_factor=inference_scale_factor)

        if self.posterior is None:
            self.posterior = MLP(self.dim_in, self.dim_h, self.dim_h, 1,
                                 rng=self.rng, trng=self.trng,
                                 h_act='T.nnet.sigmoid',
                                 out_act='lambda x: x')
        if self.conditional is None:
            self.conditional = MLP(self.dim_h, self.dim_out, self.dim_out, 1,
                                   rng=self.rng, trng=self.trng,
                                   h_act='T.nnet.sigmoid',
                                   out_act='T.nnet.sigmoid')

        self.posterior.name = self.name + '_posterior'
        self.conditional.name = self.name + '_conditional'

    def set_tparams(self, excludes=[]):
        excludes.append('inference_scale_factor')
        print 'Excluding log sigma from learned params'
        excludes.append('log_sigma')
        excludes = ['{name}_{key}'.format(name=self.name, key=key)
                    for key in excludes]
        tparams = super(GaussianBeliefNet, self).set_tparams()
        tparams.update(**self.posterior.set_tparams())
        tparams.update(**self.conditional.set_tparams())
        tparams = OrderedDict((k, v) for k, v in tparams.iteritems()
            if k not in excludes)

        return tparams

    def _sample(self, p, size=None):
        if size is None:
            size = p.shape
        return self.trng.binomial(p=p, size=size, n=1, dtype=p.dtype)

    def _noise(self, x, amount, size):
        return x * (1 - self.trng.binomial(p=amount, size=size, n=1,
                                           dtype=x.dtype))

    def set_input(self, x, mode, size=None):
        if size is None:
            size = x.shape
        if mode == 'sample':
            x = self._sample(x[None, :, :], size=size)
        elif mode == 'noise':
            x = self._sample(x)
            x = self._noise(x[None, :, :], size=size)
        elif mode is None:
            x = self._sample(x, size=x.shape)
            x = T.alloc(0., *size) + x[None, :, :]
        else:
            raise ValueError('% not supported' % mode)
        return x

    def init_inputs(self, x, y, steps=1):
        x_size = (steps, x.shape[0], x.shape[1])
        y_size = (steps, y.shape[0], y.shape[1])

        x = self.set_input(x, self.x_mode, size=x_size)
        y = self.set_input(y, self.y_mode, size=y_size)
        return x, y

    def get_params(self):
        params = [self.mu, self.log_sigma] + self.conditional.get_params() + [self.inference_scale_factor]
        return params

    def p_y_given_h(self, h, *params):
        params = params[2:-1]
        return self.conditional.step_call(h, *params)

    def sample_from_prior(self, n_samples=100):
        p = T.concatenate([self.mu, self.log_sigma])
        h = self.posterior.sample(p=p, size=(n_samples, self.dim_h))
        py = self.conditional(h)
        return py

    def m_step(self, ph, y, q, n_samples=10):
        constants = []
        prior = T.concatenate([self.mu[None, :], self.log_sigma[None, :]], axis=1)

        if n_samples == 0:
            h = mu[None, :, :]
        else:
            h = self.posterior.sample(p=q, size=(n_samples, q.shape[0], q.shape[1] / 2))

        py = self.conditional(h)
        py_approx = py

        #prior_energy = self.posterior.neg_log_prob(h, T.concatenate([self.mu, self.log_sigma])[None, None, :]).mean()
        #h_energy = self.posterior.neg_log_prob(h, ph[None, :, :]).mean()
        y_energy = self.conditional.neg_log_prob(y[None, :, :], py).mean()
        y_energy_approx = self.conditional.neg_log_prob(y, py_approx).mean()
        prior_energy = self.kl_divergence(q, prior).mean()
        h_energy = self.kl_divergence(q, ph).mean()

        entropy = self.posterior.entropy(q).mean()

        return (prior_energy, h_energy, y_energy, y_energy_approx, entropy), constants

    def kl_divergence(self, p, q,
                      entropy_scale=1.0):
        dim = self.dim_h
        mu_p = _slice(p, 0, dim)
        log_sigma_p = _slice(p, 1, dim)
        mu_q = _slice(q, 0, dim)
        log_sigma_q = _slice(q, 1, dim)

        kl = log_sigma_q - log_sigma_p + 0.5 * (
            (T.exp(2 * log_sigma_p) + (mu_p - mu_q) ** 2) /
            T.exp(2 * log_sigma_q)
            - 1)
        return kl.sum(axis=kl.ndim-1)

    def e_step(self, y, q, *params):
        prior = T.concatenate([params[0][None, :], params[1][None, :]], axis=1)

        mu_q = _slice(q, 0, self.dim_h)
        log_sigma_q = _slice(q, 1, self.dim_h)

        epsilon = self.trng.normal(
            avg=0, std=1.0,
            size=(self.n_inference_samples, mu_q.shape[0], mu_q.shape[1]))

        h = mu_q + epsilon * T.exp(log_sigma_q)
        py = self.p_y_given_h(h, *params)

        consider_constant = [y] + list(params[:1])
        cond_term = self.conditional.neg_log_prob(y[None, :, :], py).mean()

        kl_term = self.kl_divergence(q, prior)

        cost = (cond_term + kl_term).sum(axis=0)
        grad = theano.grad(cost, wrt=q, consider_constant=consider_constant)

        return cost, grad

    def step_infer(self, *params):
        raise NotImplementedError()

    def init_infer(self, q):
        raise NotImplementedError()

    def unpack_infer(self, outs):
        raise NotImplementedError()

    def params_infer(self):
        raise NotImplementedError()

    # Momentum
    def _step_momentum(self, y, q, l, dq_, m, *params):
        cost, grad = self.e_step(y, q, *params)
        dq = (-l * grad + m * dq_).astype(floatX)
        q = (q + dq).astype(floatX)
        l *= self.inference_decay
        return q, l, dq, cost

    def _init_momentum(self, ph, y, q):
        return [self.inference_rate, T.zeros_like(q)]

    def _unpack_momentum(self, outs):
        qs, ls, dqs, costs = outs
        return qs, costs

    def _params_momentum(self):
        return [T.constant(self.momentum).astype('float32')]

    def infer_q(self, x, y, n_inference_steps, q0=None):
        updates = theano.OrderedUpdates()

        xs, ys = self.init_inputs(x, y, steps=n_inference_steps)
        ph = self.posterior(xs)
        if q0 is None:
            if self.z_init == 'recognition_net':
                print 'Starting q0 at recognition net'
                q0 = ph[0]
            else:
                q0 = T.alloc(0., x.shape[0], 2 * self.dim_h).astype(floatX)

        seqs = [ys]
        outputs_info = [q0] + self.init_infer(ph[0], ys[0], q0) + [None]
        non_seqs = self.params_infer() + self.get_params()

        outs, updates_2 = theano.scan(
            self.step_infer,
            sequences=seqs,
            outputs_info=outputs_info,
            non_sequences=non_seqs,
            name=tools._p(self.name, 'infer'),
            n_steps=n_inference_steps,
            profile=tools.profile,
            strict=True
        )
        updates.update(updates_2)

        qs, i_costs = self.unpack_infer(outs)
        qs = T.concatenate([q0[None, :, :], qs], axis=0)

        return (ph, xs, ys, qs), updates

    # Inference
    def inference(self, x, y, q0=None, n_inference_steps=20, n_samples=100):
        (ph, xs, ys, qs), updates = self.infer_q(
            x, y, n_inference_steps, q0=q0)

        (prior_energy, h_energy, y_energy,
         y_energy_approx, entropy), m_constants = self.m_step(
            ph[0], ys[0], qs[-1], n_samples=n_samples)

        constants = [xs, ys, qs, entropy] + m_constants

        return (xs, ys, qs,
                prior_energy, h_energy, y_energy,
                y_energy_approx, entropy), updates, constants

    def __call__(self, x, y, ph=None, n_samples=100,
                 n_inference_steps=0, end_with_inference=True):

        outs = OrderedDict()
        updates = theano.OrderedUpdates()
        prior = T.concatenate([self.mu[None, :], self.log_sigma[None, :]], axis=1)

        if end_with_inference:
            if ph is None:
                q0 = None
            else:
                q0 = ph

            (ph_x, xs, ys, qs), updates_i = self.infer_q(
                x, y, n_inference_steps, q0=q0)
            updates.update(updates_i)
            q = qs[-1]
        elif ph is None:
            x = self.trng.binomial(p=x, size=x.shape, n=1, dtype=x.dtype)
            q = self.posterior(x)
            ys = x.copy()
        else:
            ys = x.copy()

        if n_samples == 0:
            h = q[None, :, :]
        else:
            h = self.posterior.sample(
                q, size=(n_samples, q.shape[0], q.shape[1] / 2))

        py = self.conditional(h)
        y_energy = self.conditional.neg_log_prob(ys, py).mean(axis=(0, 1))
        kl_term = self.kl_divergence(q, prior).mean(axis=0)

        outs.update(
            py=py,
            lower_bound=(y_energy+kl_term)
        )

        return outs, updates

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
from tools import concatenate
from tools import init_rngs
from tools import init_weights
from tools import log_mean_exp
from tools import log_sum_exp
from tools import logit
from tools import _slice


norm_weight = tools.norm_weight
ortho_weight = tools.ortho_weight
floatX = 'float32' # theano.config.floatX
pi = theano.shared(np.pi).astype('float32')

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
                        alpha=7,
                        center_latent=False,
                        extra_inference_args=dict(),
                        **kwargs):
    model.inference_rate = inference_rate
    model.inference_decay = inference_decay
    model.entropy_scale = entropy_scale
    model.importance_sampling = importance_sampling
    model.inference_scaling = inference_scaling
    model.n_inference_samples = n_inference_samples
    model.alpha = alpha
    model.center_latent = center_latent

    if inference_method == 'sgd':
        model.step_infer = model._step_sgd
        model.init_infer = model._init_sgd
        model.unpack_infer = model._unpack_sgd
        model.params_infer = model._params_sgd
        kwargs = init_sgd_args(model, **extra_inference_args)
    elif inference_method == 'momentum':
        model.step_infer = model._step_momentum
        model.init_infer = model._init_momentum
        model.unpack_infer = model._unpack_momentum
        model.params_infer = model._params_momentum
        kwargs = init_momentum_args(model, **extra_inference_args)
    elif inference_method == 'momentum_straight_through':
        model.step_infer = model._step_momentum_st
        model.init_infer = model._init_momentum
        model.unpack_infer = model._unpack_momentum
        model.params_infer = model._params_momentum
        kwargs = init_momentum_args(model, **extra_inference_args)
    elif inference_method == 'adaptive':
        model.step_infer = model._step_adapt
        model.init_infer = model._init_adapt
        model.unpack_infer = model._unpack_adapt
        model.params_infer = model._params_adapt
        model.strict = False
        model.init_variational_params = model._init_variational_params_adapt
    elif inference_method == 'momentum_then_adapt':
        model.step_infer = model._step_momentum_then_adapt
        model.init_infer = model._init_momentum
        model.unpack_infer = model._unpack_momentum_then_adapt
        model.params_infer = model._params_momentum
        model.init_variational_params = model._init_variational_params_adapt
        model.strict = False
        kwargs = init_momentum_args(model, **extra_inference_args)
    else:
        raise ValueError()

    return kwargs

def _sample(p, size=None, trng=None):
    if size is None:
        size = p.shape
    return trng.binomial(p=p, size=size, n=1, dtype=p.dtype)

def _noise(x, amount=0.1, size=None, trng=None):
    if size is None:
        size = x.shape
    return x * (1 - trng.binomial(p=amount, size=size, n=1, dtype=x.dtype))

def set_input(x, mode, trng=None):
    if mode == 'sample':
        x = _sample(x, trng=trng)
    elif mode == 'noise':
        x = _noise(x, trng=trng)
    elif mode is None:
        pass
    else:
        raise ValueError('% not supported' % mode)
    return x


class SigmoidBeliefNetwork(Layer):
    def __init__(self, dim_in, dim_h, dim_out,
                 posterior=None, conditional=None,
                 z_init=None,
                 name='sbn',
                 **kwargs):

        self.dim_in = dim_in
        self.dim_h = dim_h
        self.dim_out = dim_out

        self.posterior = posterior
        self.conditional = conditional

        self.z_init = z_init

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

    def get_params(self):
        params = [self.z] + self.conditional.get_params() + self.posterior.get_params() + [self.inference_scale_factor]
        return params

    def p_y_given_h(self, h, *params):
        params = params[1:1+len(self.conditional.get_params())]
        return self.conditional.step_call(h, *params)

    def sample_from_prior(self, n_samples=100):
        p = T.nnet.sigmoid(self.z)
        h = self.posterior.sample(p=p, size=(n_samples, self.dim_h))
        if self.center_latent:
            py = self.conditional(h - p[None, :])
        else:
            py = self.conditional(h)
        return py

    def importance_weights(self, y, h, py, q, prior, normalize=True):
        y_energy = self.conditional.neg_log_prob(y, py)
        prior_energy = self.posterior.neg_log_prob(h, prior)
        entropy_term = self.posterior.neg_log_prob(h, q)

        log_p = -y_energy - prior_energy + entropy_term
        log_p_max = T.max(log_p, axis=0, keepdims=True)
        w = T.exp(log_p - log_p_max)

        if normalize:
            w = w / w.sum(axis=0, keepdims=True)

        return w

    def log_marginal(self, y, h, py, q, prior):
        y_energy = self.conditional.neg_log_prob(y, py)
        prior_energy = self.posterior.neg_log_prob(h, prior)
        entropy_term = self.posterior.neg_log_prob(h, q)

        log_p = -y_energy - prior_energy + entropy_term
        log_p_max = T.max(log_p, axis=0, keepdims=True)
        w = T.exp(log_p - log_p_max)

        return (T.log(w.mean(axis=0, keepdims=True)) + log_p_max).mean()

    def kl_divergence(self, p, q):
        entropy_term = self.posterior.entropy(p)
        prior_term = self.posterior.neg_log_prob(p, q)
        return prior_term - entropy_term

    def e_step(self, y, z, *params):
        prior = T.nnet.sigmoid(params[0])
        q = T.nnet.sigmoid(z)

        if self.center_latent:
            print 'E step: Centering binary latent variables before passing to generation net'
            py  = self.p_y_given_h(q - prior[None, :], *params)
        else:
            py = self.p_y_given_h(q, *params)

        consider_constant = [y, prior]
        cond_term = self.conditional.neg_log_prob(y, py)

        kl_term = self.kl_divergence(q, prior[None, :])
        cost = (cond_term + kl_term).sum(axis=0)

        grad = theano.grad(cost, wrt=z, consider_constant=consider_constant)

        return cost, grad

    def m_step(self, x, y, z, n_samples=10):
        constants = []
        q = T.nnet.sigmoid(z)
        prior = T.nnet.sigmoid(self.z)
        p_h = self.posterior(x)

        if n_samples == 0:
            h = q[None, :, :]
        else:
            h = self.posterior.sample(
                q, size=(n_samples, q.shape[0], q.shape[1]))

        if self.center_latent:
            print 'M step: Centering binary latent variables before passing to generation net'
            py = self.conditional(h - prior[None, None, :])
        else:
            py = self.conditional(h)

        entropy = self.posterior.entropy(q).mean()

        prior_energy = self.posterior.neg_log_prob(q, prior[None, :]).mean()
        y_energy = self.conditional.neg_log_prob(y[None, :, :], py).mean()
        h_energy = self.posterior.neg_log_prob(q, p_h).mean()

        return (prior_energy, h_energy, y_energy, entropy), constants

    def step_infer(self, *params):
        raise NotImplementedError()

    def init_infer(self, z):
        raise NotImplementedError()

    def unpack_infer(self, outs):
        raise NotImplementedError()

    def params_infer(self):
        raise NotImplementedError()

    # Importance Sampling
    def _step_adapt(self, y, q, *params):
        prior = T.nnet.sigmoid(params[0])
        h = self.posterior.sample(
            q, size=(self.n_inference_samples, q.shape[0], q.shape[1]))

        if self.center_latent:
            py = self.p_y_given_h(h - prior[None, None, :], *params)
        else:
            py = self.p_y_given_h(h, *params)

        '''
        w = self.importance_weights(
            y[None, :, :], h, py, q[None, :, :], prior[None, None, :])
        '''

        y_energy = self.conditional.neg_log_prob(y[None, :, :], py)
        prior_energy = self.posterior.neg_log_prob(h, prior[None, None, :])
        entropy_term = self.posterior.neg_log_prob(h, q[None, :, :])

        log_p = -y_energy - prior_energy + entropy_term
        log_p_max = T.max(log_p, axis=0, keepdims=True)
        w = T.exp(log_p - log_p_max)

        w_tilde = w / w.sum(axis=0, keepdims=True)

        cost = (log_p - log_p_max).mean()
        q = self.inference_rate * (w_tilde[:, :, None] * h).sum(axis=0) + (1 - self.inference_rate) * q

        return q, cost

    def _init_adapt(self, q):
        return []

    def _init_variational_params_adapt(self, p_h_logit):
        if self.z_init == 'recognition_net':
            print 'Starting z0 at recognition net'
            q0 = T.nnet.sigmoid(p_h_logit)
        else:
            q0 = T.alloc(0.5, p_h_logit.shape[0], self.dim_h).astype(floatX)

        return q0

    def _unpack_adapt(self, q0, outs):
        if outs is not None:
            qs, costs = outs
            if qs.ndim == 2:
                qs = concatenate([q0[None, :, :], qs[None, :, :]], axis=0)
                costs = [costs]
            else:
                qs = T.concatenate([q0[None, :, :], qs])

        else:
            qs = q0[None, :, :]
            costs = [T.constant(0.).astype(floatX)]
        return logit(qs), costs

    def _params_adapt(self):
        return []

    # SGD
    def _step_sgd(self, y, z, l, *params):
        cost, grad = self.e_step(y, z, *params)
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
    def _step_momentum(self, y, z, dz_, m, *params):
        l = self.inference_rate
        cost, grad = self.e_step(y, z, *params)
        dz = (-l * grad + m * dz_).astype(floatX)
        z = (z + dz).astype(floatX)
        return z, dz, cost

    def _step_momentum_st(self, y, z, l, dz_, m, *params):
        prior = T.nnet.sigmoid(params[0])
        consider_constant = [y, prior]

        q = T.nnet.sigmoid(z)

        h = self.posterior.sample(q, size=(self.n_inference_samples, q.shape[0], q.shape[1]))

        if self.center_latent:
            py = self.p_y_given_h(h - prior[None, None, :], *params)
        else:
            py = self.p_y_given_h(h, *params)

        kl_term = self.kl_divergence(q, prior[None, :])
        cond_term = self.conditional.neg_log_prob(y[None, :, :], py).mean(axis=0)

        grad_h = theano.grad(cond_term.sum(axis=0), wrt=h, consider_constant=consider_constant)
        #grad_q = (grad_h * q * (1 - q)).sum(axis=0)
        grad_q = grad_h.sum(axis=0)

        grad_k = theano.grad(kl_term.sum(axis=0), wrt=z, consider_constant=consider_constant)
        grad = grad_q + grad_k

        dz = (-l * grad + m * dz_).astype(floatX)
        z = (z + dz).astype(floatX)
        l *= self.inference_decay

        return z, l, dz, (grad).mean()

    def _init_momentum(self, z):
        return [T.zeros_like(z)]

    def _unpack_momentum(self, z0, outs):
        if outs is not None:
            zs, dzs, costs = outs
            if zs.ndim == 2:
                zs = zs[None, :, :]
                costs = costs[None, :]
            zs = concatenate([z0[None, :, :], zs])
        else:
            zs = z0[None, :, :]
            costs = [T.constant(0.).astype(floatX)]
        return zs, costs

    def _unpack_momentum_then_adapt(self, outs):
        qs, ls, dqs, costs = outs
        return logit(qs), costs

    def _params_momentum(self):
        return [T.constant(self.momentum).astype('float32')]

    def init_variational_params(self, p_h_logit):
        if self.z_init == 'recognition_net':
            print 'Starting z0 at recognition net'
            z0 = p_h_logit
        else:
            z0 = T.alloc(0., p_h_logit.shape[0], self.dim_h).astype(floatX)

        return z0

    def infer_q(self, x, y, n_inference_steps):
        updates = theano.OrderedUpdates()

        ys = T.alloc(0., n_inference_steps + 1, y.shape[0], y.shape[1]) + y[None, :, :]

        p_h_logit = self.posterior(x, return_preact=True)
        z0 = self.init_variational_params(p_h_logit)

        seqs = [ys]
        outputs_info = [z0] + self.init_infer(z0) + [None]
        non_seqs = self.params_infer() + self.get_params()

        if isinstance(n_inference_steps, T.TensorVariable) or n_inference_steps > 1:
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

            zs, i_costs = self.unpack_infer(z0, outs)

        elif n_inference_steps == 1:
            inps = [ys[0]] + outputs_info[:-1] + non_seqs
            outs = self.step_infer(*inps)
            zs, i_costs = self.unpack_infer(z0, outs)

        elif n_inference_steps == 0:
            zs, i_costs = self.unpack_infer(z0, None)

        return (zs, i_costs), updates

    # Inference
    def inference(self, x, y, n_inference_steps=20, n_samples=100):

        (zs, _), updates = self.infer_q(x, y, n_inference_steps)

        z = zs[-1]

        (prior_energy, h_energy, y_energy, entropy), m_constants = self.m_step(
            x, y, z, n_samples=n_samples)

        constants = [z, entropy] + m_constants

        return (z, prior_energy, h_energy, y_energy, entropy), updates, constants

    def __call__(self, x, y, n_samples=100, n_inference_steps=0,
                 calculate_log_marginal=False):
        outs = OrderedDict()
        updates = theano.OrderedUpdates()
        prior = T.nnet.sigmoid(self.z)

        (zs, i_costs), updates_i = self.infer_q(x, y, n_inference_steps)
        updates.update(updates_i)

        lower_bounds = []
        for i in xrange(n_inference_steps + 1):
            z = zs[i]
            q = T.nnet.sigmoid(z)
            if n_samples == 0:
                h = q[None, :, :]
            else:
                h = self.posterior.sample(
                    q, size=(n_samples, q.shape[0], q.shape[1]))

            if self.center_latent:
                print 'Centering latents in call'
                py = self.conditional(h - prior[None, None, :])
            else:
                py = self.conditional(h)

            cond_term = self.conditional.neg_log_prob(y[None, :, :], py).mean()
            kl_term = self.kl_divergence(q, prior[None, :]).mean()
            lower_bounds.append(cond_term + kl_term)

        outs.update(
            py=py,
            lower_bound=lower_bounds[-1],
            inference_cost=(lower_bounds[0] - lower_bounds[-1])
        )

        if calculate_log_marginal:
            nll = -self.log_marginal(y[None, :, :], h, py, q[None, :, :], prior[None, None, :])
            outs.update(nll=nll)

        return outs, updates


#Deep Sigmoid Belief Networks===================================================


class DeepSBN(Layer):
    def __init__(self, dim_in, dim_h, dim_out, n_layers=2,
                 posteriors=None, conditionals=None,
                 z_init=None,
                 name='sbn',
                 **kwargs):

        self.dim_in = dim_in
        self.dim_h = dim_h
        self.dim_out = dim_out

        self.n_layers = n_layers

        self.posteriors = posteriors
        self.conditionals = conditionals

        self.z_init = z_init

        kwargs = init_inference_args(self, **kwargs)
        kwargs = init_weights(self, **kwargs)
        kwargs = init_rngs(self, **kwargs)

        super(DeepSBN, self).__init__(name=name)

    def set_params(self):
        z = np.zeros((self.dim_h,)).astype(floatX)

        self.params = OrderedDict(z=z)

        if self.posteriors is None:
            self.posteriors = [None for _ in xrange(self.n_layers)]
        else:
            assert len(self.posteriors) == self.n_layers

        if self.conditionals is None:
            self.conditionals = [None for _ in xrange(self.n_layers)]
        else:
            assert len(self.conditionals) == self.n_layers

        for l in xrange(self.n_layers):
            if l == 0:
                dim_in = self.dim_in
            else:
                dim_in = self.dim_h

            if l == self.n_layers - 1:
                dim_out = self.dim_out
            else:
                dim_out = self.dim_h

            if self.posteriors[l] is None:
                self.posteriors[l] = MLP(
                    dim_in, dim_out, dim_out, 1,
                    rng=self.rng, trng=self.trng,
                    h_act='T.nnet.sigmoid',
                    out_act='T.nnet.sigmoid')

            if self.conditionals[l] is None:
                self.conditionals[l] = MLP(
                    dim_out, dim_out, dim_in, 1,
                    rng=self.rng, trng=self.trng,
                    h_act='T.nnet.sigmoid',
                    out_act='T.nnet.sigmoid')

            if l == 0:
                self.posteriors[l].name = self.name + '_posterior'
                self.conditionals[l].name = self.name + '_conditional'
            else:
                self.posteriors[l].name = self.name + '_posterior%d' % l
                self.conditionals[l].name = self.name + '_conditional%d' % l

    def set_tparams(self, excludes=[]):
        excludes = ['{name}_{key}'.format(name=self.name, key=key)
                    for key in excludes]
        tparams = super(DeepSBN, self).set_tparams()

        for l in xrange(self.n_layers):
            tparams.update(**self.posteriors[l].set_tparams())
            tparams.update(**self.conditionals[l].set_tparams())

        tparams = OrderedDict((k, v) for k, v in tparams.iteritems()
            if k not in excludes)

        return tparams

    def get_params(self):
        params = [self.z]
        for l in xrange(self.n_layers):
            params += self.conditionals[l].get_params()
        return params

    def p_y_given_h(self, h, level, *params):
        start = 1
        for l in xrange(level):
            start += len(self.conditionals[l].get_params())
        end = start + len(self.conditionals[level].get_params())

        params = params[start:end]
        return self.conditionals[level].step_call(h, *params)

    def sample_from_prior(self, n_samples=100):
        p = T.nnet.sigmoid(self.z)
        h = self.posteriors[-1].sample(p=p, size=(n_samples, self.dim_h))

        for conditional in self.conditionals[::-1]:
            p = conditional(h)
            h = conditional.sample(p)

        return p

    def kl_divergence(self, p, q):
        '''
        Negative KL divergence actually.
        '''
        p_c = T.clip(p, 1e-7, 1.0 - 1e-7)
        q = T.clip(q, 1e-7, 1.0 - 1e-7)

        entropy_term = T.nnet.binary_crossentropy(p_c, p)
        prior_term = T.nnet.binary_crossentropy(q, p)
        return (prior_term - entropy_term).sum(axis=entropy_term.ndim-1)

    def e_step(self, y, zs, *params):
        total_cost = T.constant(0.).astype(floatX)
        qs = [T.nnet.sigmoid(z) for z in zs]

        prior = T.nnet.sigmoid(params[0])
        ys = [y] + qs[:-1]

        hs = []
        for l, q in enumerate(qs):
            h = self.posteriors[l].sample(
                q, size=(self.n_inference_samples, q.shape[0], q.shape[1]))
            hs.append(h)

        p_ys = [self.p_y_given_h(h, l, *params) for l, h in enumerate(hs)]
        p_y_approxs = [self.p_y_given_h(q, l, *params) for l, h in enumerate(qs)]

        grads = []
        def refine_layer(l):
            z = zs[l]
            q = qs[l]
            y = ys[l]
            h = hs[l]

            if l == self.n_layers - 1:
                kl_term = self.kl_divergence(q, prior[None, :])
            else:
                kl_term = self.kl_divergence(q[None, :, :], p_ys[l + 1]).mean(axis=0)

            cond_term = self.conditionals[l].neg_log_prob(y, p_y_approxs[l])

            cost = (cond_term + kl_term).sum(axis=0)

            grad = theano.grad(cost, wrt=z, consider_constant=[y, prior])
            return grad, cost

        for l in xrange(self.n_layers):
            grad, cost = refine_layer(l)
            grads.append(grad)
            total_cost += cost

        return total_cost, grads

    def m_step(self, x, y, zs, n_samples=10):
        constants = []

        qs = [T.nnet.sigmoid(z) for z in zs]
        ys = [y] + qs[:-1]

        hs = []
        for l, q in enumerate(qs):
            h = self.posteriors[l].sample(q, size=(n_samples, q.shape[0], q.shape[1]))
            hs.append(h)
        p_ys = [conditional(h) for h, conditional in zip(hs, self.conditionals)]

        p_hs = []
        state = x
        for l, posterior in enumerate(self.posteriors):
            state = posterior(state)
            p_hs.append(state)

        conditional_energy = T.constant(0.).astype(floatX)
        posterior_energy = T.constant(0.).astype(floatX)

        for l in xrange(self.n_layers):
            posterior_energy += self.posteriors[l].neg_log_prob(qs[l], p_hs[l])
            conditional_energy += self.conditionals[l].neg_log_prob(
                ys[l][None, :, :], p_ys[l]).mean(axis=0)

        prior = T.nnet.sigmoid(self.z)
        prior_energy = self.posteriors[-1].neg_log_prob(qs[-1], prior[None, :])

        return (prior_energy.mean(axis=0), posterior_energy.mean(axis=0),
                conditional_energy.mean(axis=0)), constants

    def step_infer(self, *params):
        raise NotImplementedError()

    def init_infer(self, z):
        raise NotImplementedError()

    def unpack_infer(self, outs):
        raise NotImplementedError()

    def params_infer(self):
        raise NotImplementedError()

    # Importance Sampling
    def _step_adapt(self, y, *params):
        params = list(params)
        qs = params[:self.n_layers]
        params = params[self.n_layers:]
        prior = T.nnet.sigmoid(params[0])

        hs = []
        new_qs = []

        for l, q in enumerate(qs):
            h = self.posteriors[l].sample(
                q, size=(self.n_inference_samples, q.shape[0], q.shape[1]))
            hs.append(h)

        ys = [y] + qs[:-1]
        p_ys = [self.p_y_given_h(h, l, *params) for l, h in enumerate(hs)]

        log_w = -self.posteriors[-1].neg_log_prob(hs[-1], prior[None, None, :])

        for l in xrange(self.n_layers):
            cond_term = -self.conditionals[l].neg_log_prob(ys[l][None, :, :], p_ys[l])
            post_term = -self.posteriors[l].neg_log_prob(hs[l], qs[l][None, :, :])
            log_w += cond_term - post_term

        log_w_max = T.max(log_w, axis=0, keepdims=True)
        w = T.exp(log_w - log_w_max)
        w_tilde = w / w.sum(axis=0, keepdims=True)

        for l in xrange(self.n_layers):
            h = hs[l]
            q = (w_tilde[:, :, None] * h).sum(axis=0)
            new_qs.append((1.0 - self.inference_rate) * qs[l] + self.inference_rate * q)

        cost = -T.log(w).mean()

        return tuple(new_qs) + (cost,)

    def _init_adapt(self, qs):
        return []

    def _init_variational_params_adapt(self, state):
        print 'Initializing variational params for AdIS'
        q0s = []

        for l in xrange(self.n_layers):
            state = self.posteriors[l](state)
            q0s.append(state)

        return q0s

    def _unpack_adapt(self, z0s, outs):
        qss = outs[:self.n_layers]
        qss = [concatenate([z0[None, :, :], qs]) for z0, qs in zip(z0s, qss)]
        return [logit(qs) for qs in qss], outs[-1]

    def _params_adapt(self):
        return []

    # Momentum
    def _step_momentum(self, y, *params):
        l = self.inference_rate
        params = list(params)
        zs = params[:self.n_layers]
        dzs_ = params[self.n_layers:2*self.n_layers]
        m = params[2*self.n_layers]
        params = params[1+2*self.n_layers:]

        cost, grads = self.e_step(y, zs, *params)

        dzs = [(-l * grad + m * dz_).astype(floatX) for dz_, grad in zip(dzs_, grads)]
        zs = [(z + dz).astype(floatX) for z, dz in zip(zs, dzs)]

        return tuple(zs + dzs) + (cost,)

    def _init_momentum(self, zs):
        return [T.zeros_like(z) for z in zs]

    def _unpack_momentum(self, z0s, outs):
        zss = outs[:self.n_layers]
        zss = [concatenate([z0[None, :, :], zs]) for z0, zs in zip(z0s, zss)]
        return zss, outs[-1]

    def _params_momentum(self):
        return [T.constant(self.momentum).astype('float32')]

    def init_variational_params(self, state):
        z0s = []

        for l in xrange(self.n_layers):
            p_h_logit = self.posteriors[l](state, return_preact=True)
            state = T.nnet.sigmoid(p_h_logit)
            z0s.append(p_h_logit)

        return z0s

    def infer_q(self, x, y, n_inference_steps, n_sampling_steps=0):
        updates = theano.OrderedUpdates()

        ys = T.alloc(0., n_inference_steps + 1, y.shape[0], y.shape[1]) + y[None, :, :]
        z0s = self.init_variational_params(x)

        seqs = [ys]
        outputs_info = z0s + self.init_infer(z0s) + [None]
        non_seqs = self.params_infer() + self.get_params()

        if isinstance(n_inference_steps, T.TensorVariable) or n_inference_steps > 1:
            outs, updates = theano.scan(
                self.step_infer,
                sequences=seqs,
                outputs_info=outputs_info,
                non_sequences=non_seqs,
                name=tools._p(self.name, 'infer'),
                n_steps=n_inference_steps,
                profile=tools.profile,
                strict=True
            )

            zss, i_costs = self.unpack_infer(z0s, outs)
        else:
            raise NotImplementedError()

        #zs = [z[-1] for z in zss]

        return (zss, i_costs), updates

    # Inference
    def inference(self, x, y, n_inference_steps=20,
                  n_sampling_steps=0, n_samples=100):

        (zss, _), updates = self.infer_q(
            x, y, n_inference_steps, n_sampling_steps=n_sampling_steps)

        zs = [z[-1] for z in zss]

        (prior_energy, h_energy, y_energy), m_constants = self.m_step(
            x, y, zs, n_samples=n_samples)

        constants = zs + m_constants

        return (zs, prior_energy, h_energy, y_energy), updates, constants

    def __call__(self, x, y,
                 n_samples=100, n_inference_steps=0, n_sampling_steps=0,
                 calculate_log_marginal=False):

        outs = OrderedDict()
        updates = theano.OrderedUpdates()

        (zss, i_costs), updates_i = self.infer_q(
            x, y, n_inference_steps, n_sampling_steps=n_sampling_steps)
        updates.update(updates_i)

        lower_bounds = []

        def get_lower_bound(step):
            zs = [z[step] for z in zss]
            qs = [T.nnet.sigmoid(z) for z in zs]

            lower_bound = T.constant(0.).astype(floatX)

            hs = []
            for l, q in enumerate(qs):
                if n_samples == 0:
                    h = q[None, :, :]
                else:
                    h = self.posteriors[l].sample(
                        q, size=(n_samples, q.shape[0], q.shape[1]))
                hs.append(h)

            ys = [y] + qs[:-1]
            p_ys = [conditional(h) for h, conditional in zip(hs, self.conditionals)]
            prior = T.nnet.sigmoid(self.z)

            for l in xrange(self.n_layers):
                q = qs[l]
                y_ = ys[l]

                if l == self.n_layers - 1:
                    kl_term = self.kl_divergence(q, prior[None, :])
                else:
                    kl_term = -self.posteriors[l].entropy(q)

                cond_term = self.conditionals[l].neg_log_prob(y_[None, :, :], p_ys[l]).mean(axis=0)

                lower_bound += (kl_term + cond_term).mean(axis=0)

            return lower_bound, p_ys[0]

        for l in xrange(n_inference_steps):
            lower_bound, p_y = get_lower_bound(l)
            lower_bounds.append(lower_bound)

        outs.update(
            py=p_y,
            lower_bound=lower_bounds[-1],
            lower_bounds=lower_bounds,
            lower_bound_gain=(lower_bounds[0] - lower_bounds[-1])
        )

        return outs, updates

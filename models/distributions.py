'''
Module for Theano probabilistic distributions.
'''

from collections import OrderedDict
import numpy as np
import theano
from theano import tensor as T

from models.layers import Layer
from utils.tools import (
    concatenate,
    e,
    floatX,
    init_rngs,
    init_weights,
    pi,
    _slice
)


_clip = 1e-7


def resolve(c, conditional=False):
    if conditional:
        if c == 'binomial':
            return Binomial
        elif c == 'continuous_binomial':
            return ContinuousBinomial
        elif c == 'centered_binomial':
            return CenteredBinomial
        elif c == 'multinomial':
            return Multinomial
        elif c == 'gaussian':
            return Gaussian
        elif c == 'truncated_gaussian':
            return TruncatedGaussian
        else:
            raise ValueError(c)
    else:
        if c == 'binomial':
            return ConditionalBinomial
        elif c == 'continuous_binomial':
            return ConditionalContinuousBinomial
        elif c == 'centered_binomial':
            return ConditionalCenteredBinomial
        elif c == 'multinomial':
            return ConditionalMultinomial
        elif c == 'gaussian':
            return ConditionalGaussian
        elif c == 'truncated_gaussian':
            return ConditionalTruncatedGaussian
        else:
            raise ValueError(c)


class Distribution(Layer):
    def __init__(self, dim, name='distribution', must_sample=False, scale=1,
                 **kwargs):
        self.dim = dim
        self.must_sample = must_sample
        self.scale = scale

        kwargs = init_weights(self, **kwargs)
        kwargs = init_rngs(self, **kwargs)

        super(Distribution, self).__init__(name=name)

    def set_params(self):
        raise NotImplementedError()

    def get_params(self):
        raise NotImplementedError()

    def get_prob(self):
        raise NotImplementedError()

    def kl_divergence(self, q):
        raise NotImplementedError()

    def __call__(self, z):
        raise NotImplementedError()

    def sample(self, n_samples, p=None):
        if p is None:
            p = self.get_prob(*self.get_params())
        if p.ndim == 1:
            size = (n_samples, p.shape[0] // self.scale)
        elif p.ndim == 2:
            size = (n_samples, p.shape[0], p.shape[1] // self.scale)
        elif p.ndim == 3:
            size = (n_samples, p.shape[0], p.shape[2], p.shape[3] // self.scale)
        elif p.ndim == 4:
            raise NotImplementedError('%d dim sampling not supported yet' % p.ndim)

        return self.f_sample(self.trng, p, size=size), theano.OrderedUpdates()

    def step_neg_log_prob(self, x, p):
        return self.f_neg_log_prob(x, p)

    def neg_log_prob(self, x, p=None):
        if p is None:
            p = self.get_prob(*self.get_params())
        return self.f_neg_log_prob(x, p)

    def entropy(self, p=None):
        if p is None:
            p = self.get_prob(*self.get_params())
        return self.f_entropy(p)

    def get_center(self, p):
        return p


class Binomial(Distribution):
    def __init__(self, dim, name='binomial', **kwargs):
        self.f_sample = _binomial
        self.f_neg_log_prob = _cross_entropy
        self.f_entropy = _binary_entropy
        super(Binomial, self).__init__(dim, name=name, **kwargs)

    def set_params(self):
        z = np.zeros((self.dim,)).astype(floatX)
        self.params = OrderedDict(z=z)

    def get_params(self):
        return [self.z]

    def get_prob(self, z):
        return T.nnet.sigmoid(z) * 0.9999 + 0.000005

    def __call__(self, z):
        return T.nnet.sigmoid(z) * 0.9999 + 0.000005

    def prototype_samples(self, size):
        return self.trng.uniform(size, dtype=floatX)

class CenteredBinomial(Binomial):
    def __call__(self, z):
        return T.tanh(z)

    def neg_log_prob(self, x, p=None):
        if p is None:
            p = self.get_prob(*self.get_params())
        x = 0.5 * (x + 1)
        p = 0.5 * (p + 1)
        return self.f_neg_log_prob(x, p)


class ContinuousBinomial(Binomial):
    def sample(self, n_samples, p=None):
        if p is None:
            p = self.get_prob(*self.get_params())
        return T.shape_padleft(p), theano.OrderedUpdates()

class ConditionalBinomial(Binomial):
    def set_params(self): self.params = OrderedDict()
    def get_params(self): return []

class ConditionalCenteredBinomial(CenteredBinomial):
    def set_params(self): self.params = OrderedDict()
    def get_params(self): return []

class ConditionalContinuousBinomial(ContinuousBinomial):
    def set_params(self): self.params = OrderedDict()
    def get_params(self): return []


class Multinomial(Distribution):
    def __init(self, dim, name='multinomial', **kwargs):
        self.f_sample = _sample_multinomial
        self.f_neg_log_prob = _categorical_cross_entropy
        self.f_entropy = _categorical_entropy
        super(Multinomal, self).__init__(dim, name=name, **kwargs)

    def set_params(self):
        z = np.zeros((self.dim,)).astype(floatX)
        self.params = OrderedDict(z=z)

    def get_prob(self, z):
        return _softmax(z)

    def __call__(self, z):
        return _softmax(z)

class ConditionalMultinomial(Multinomial):
    def set_params(self): self.params = OrderedDict()
    def get_params(self): return []


class Gaussian(Distribution):
    def __init__(self, dim, name='gaussian', clip=-10, **kwargs):
        self.f_sample = _normal
        self.f_neg_log_prob = _neg_normal_log_prob
        self.f_entropy = _normal_entropy
        self.clip = clip
        super(Gaussian, self).__init__(dim, name=name, scale=2, **kwargs)

    def set_params(self):
        mu = np.zeros((self.dim,)).astype(floatX)
        log_sigma = np.zeros((self.dim,)).astype(floatX)

        self.params = OrderedDict(
            mu=mu, log_sigma=log_sigma)

    def get_params(self):
        return [self.mu, self.log_sigma]

    def get_prob(self, mu, log_sigma):
        return concatenate([mu, log_sigma])

    def __call__(self, z):
        return z

    def get_center(self, p):
        mu = _slice(p, 0, p.shape[p.ndim-1] // self.scale)
        return mu

    def split_prob(self, p):
        mu        = _slice(p, 0, p.shape[p.ndim-1] // self.scale)
        log_sigma = _slice(p, 1, p.shape[p.ndim-1] // self.scale)
        return mu, log_sigma

    def step_kl_divergence(self, q, mu, log_sigma):
        mu_q = _slice(q, 0, self.dim)
        log_sigma_q = _slice(q, 1, self.dim)
        log_sigma_q = T.maximum(log_sigma_q, self.clip)
        log_sigma = T.maximum(log_sigma, self.clip)

        kl = log_sigma - log_sigma_q + 0.5 * (
            (T.exp(2 * log_sigma_q) + (mu - mu_q) ** 2) /
            T.exp(2 * log_sigma)
            - 1)
        return kl.sum(axis=kl.ndim-1)

    def kl_divergence(self, q):
        return self.step_kl_divergence(q, *self.get_params())

    def step_sample(self, epsilon, p):
        dim = p.shape[p.ndim-1] // self.scale
        mu = _slice(p, 0, dim)
        log_sigma = _slice(p, 1, dim)
        return mu + epsilon * T.exp(log_sigma)

    def prototype_samples(self, size):
        return self.trng.normal(
            avg=0, std=1.0,
            size=size,
            dtype=floatX
        )

    def step_neg_log_prob(self, x, p):
        return self.f_neg_log_prob(x, p, clip=self.clip)

    def neg_log_prob(self, x, p=None):
        if p is None:
            p = self.get_prob(*self.get_params())
        return self.f_neg_log_prob(x, p, clip=self.clip)

    def entropy(self, p=None):
        if p is None:
            p = self.get_prob(*self.get_params())
        return self.f_entropy(p, clip=self.clip)


class TruncatedGaussian(Gaussian):
    def __init__(self, dim, name='truncated_gaussian', minmax=(0, 1), **kwargs):
        self.min, self.max = minmax
        super(TruncatedGaussian, self).__init__(dim, name=name, **kwargs)

    def get_params(self):
        return [T.clip(self.mu, self.min, self.max), self.log_sigma]

    def __call__(self, p):
        mu = _slice(p, 0, p.shape[p.ndim-1] // 2)
        log_sigma = _slice(p, 1, p.shape[p.ndim-1] // 2)
        mu = T.clip(mu, self.min, self.max)
        return concatenate([mu, log_sigma], axis=mu.ndim-1)

    def sample(self, n_samples, p=None):
        samples, updates = super(TruncatedGaussian, self).sample(n_samples, p=p)
        return T.clip(samples, self.min, self.max), updates

    def step_sample(self, epsilon, p):
        return T.clip(super(TruncatedGaussian, self).step_sample(epsilon, p),
                      self.min, self.max)

    def step_kl_divergence(self, q, mu, log_sigma):
        mu_q = _slice(q, 0, self.dim)
        mu = T.clip(mu, self.min, self.max)
        mu_q = T.clip(mu_q, self.min, self.max)
        log_sigma_q = _slice(q, 1, self.dim)

        kl = log_sigma - log_sigma_q + 0.5 * (
            (T.exp(2 * log_sigma_q) + (mu - mu_q) ** 2) /
            T.exp(2 * log_sigma)
            - 1)
        return kl.sum(axis=kl.ndim-1)

    def step_neg_log_prob(self, x, p):
        mu = _slice(p, 0, p.shape[p.ndim-1] // 2)
        log_sigma = _slice(p, 1, p.shape[p.ndim-1] // 2)
        mu = T.clip(mu, self.min, self.max)
        p = concatenate([mu, log_sigma], axis=mu.ndim-1)
        return self.f_neg_log_prob(x, p)

    def neg_log_prob(self, x, p=None):
        if p is None:
            p = self.get_prob(*self.get_params())
        mu = _slice(p, 0, p.shape[p.ndim-1] // 2)
        log_sigma = _slice(p, 1, p.shape[p.ndim-1] // 2)
        mu = T.clip(mu, self.min, self.max)
        p = concatenate([mu, log_sigma], axis=mu.ndim-1)
        return self.f_neg_log_prob(x, p)


class ConditionalGaussian(Gaussian):
    def set_params(self): self.params = OrderedDict()
    def get_params(self): return []

class ConditionalTrucatedGaussian(TruncatedGaussian):
    def set_params(self): self.params = OrderedDict()
    def get_params(self): return []


# BERNOULLI --------------------------------------------------------------------

def _binomial(trng, p, size=None):
    if size is None:
        size = p.shape
    return trng.binomial(p=p, size=size, n=1, dtype=p.dtype)

def _centered_binomial(trng, p, size=None):
    if size is None:
        size = p.shape
    return 2 * trng.binomial(p=0.5*(p+1), size=size, n=1, dtype=p.dtype) - 1.

def _cross_entropy(x, p):
    #p = T.clip(p, _clip, 1.0 - _clip)
    energy = -x * T.log(p) - (1 - x) * T.log(1 - p)
    #energy = T.nnet.binary_crossentropy(p, x)
    return energy.sum(axis=energy.ndim-1)

def _binary_entropy(p):
    #p_c = T.clip(p, _clip, 1.0 - _clip)
    entropy = -p * T.log(p) - (1 - p) * T.log(1 - p)
    #entropy = T.nnet.binary_crossentropy(p_c, p)
    return entropy.sum(axis=entropy.ndim-1)

# SOFTMAX ----------------------------------------------------------------------

def _softmax(x):
    axis = x.ndim - 1
    e_x = T.exp(x - x.max(axis=axis, keepdims=True))
    out = e_x / e_x.sum(axis=axis, keepdims=True)
    return out

def _sample_multinomial(trng, p, size=None):
    if size is None:
        size = p.shape
    return trng.multinomial(pvals=p, size=size).astype('float32')

def _categorical_cross_entropy(x, p):
    p = T.clip(p, _clip, 1.0 - _clip)
    return T.nnet.binary_crossentropy(p, x).sum(axis=x.ndim-1)

def _categorical_entropy(p):
    p_c = T.clip(p, _clip, 1.0 - _clip)
    entropy = T.nnet.categorical_crossentropy(p_c, p)
    return entropy

# GAUSSIAN ---------------------------------------------------------------------

def _normal(trng, p, size=None):
    dim = p.shape[p.ndim-1] // 2
    mu = _slice(p, 0, dim)
    log_sigma = _slice(p, 1, dim)

    if size is None:
        size = mu.shape
    return trng.normal(avg=mu, std=T.exp(log_sigma), size=size, dtype=floatX)

def _normal_prob(p):
    dim = p.shape[p.ndim-1] // 2
    mu = _slice(p, 0, dim)
    return mu

def _neg_normal_log_prob(x, p, clip=None):
    dim = p.shape[p.ndim-1] // 2
    mu = _slice(p, 0, dim)
    log_sigma = _slice(p, 1, dim)
    if clip is not None:
        log_sigma = T.maximum(log_sigma, clip)
    energy = 0.5 * (
        (x - mu)**2 / (T.exp(2 * log_sigma)) + 2 * log_sigma + T.log(2 * pi))
    return energy.sum(axis=energy.ndim-1)

def _normal_entropy(p, clip=None):
    dim = p.shape[p.ndim-1] // 2
    log_sigma = _slice(p, 1, dim)
    if clip is not None:
        log_sigma = T.maximum(log_sigma, clip)
    entropy = 0.5 * T.log(2 * pi * e) + log_sigma
    return entropy.sum(axis=entropy.ndim-1)

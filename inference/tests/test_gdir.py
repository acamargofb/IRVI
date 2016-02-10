'''
Test GDIR
'''

import numpy as np
import theano
from theano import tensor as T

from datasets.mnist import MNIST
from inference.gdir import MomentumGDIR
from models.gbn import GBN
from models.tests import test_vae
from utils.tools import floatX


def test_build_gdir(model=None):
    if model is None:
        data_iter = MNIST(source='/Users/devon/Data/mnist.pkl.gz', batch_size=27)
        model = test_vae.test_build_GBN(dim_in=data_iter.dims[data_iter.name])
    gdir = MomentumGDIR(model)
    return gdir

def test_infer():
    data_iter = MNIST(source='/Users/devon/Data/mnist.pkl.gz', batch_size=27)
    gbn = test_vae.test_build_GBN(dim_in=data_iter.dims[data_iter.name])

    gdir = test_build_gdir(gbn)

    X = T.matrix('x', dtype=floatX)

    inference_args = dict(
        n_inference_samples=13,
        n_inference_steps=17,
        pass_gradients=True
    )

    rval, constants, updates = gdir.inference(X, X, **inference_args)

    f = theano.function([X], rval.values(), updates=updates)
    x = data_iter.next()[data_iter.name]

    results, samples, full_results, updates = gdir(X, X, **inference_args)
    f = theano.function([X], results.values(), updates=updates)

    print f(x)


'''
Sampling and inference with LSTM models
'''

import argparse
from collections import OrderedDict
from glob import glob
import matplotlib
from matplotlib import animation
from matplotlib import pylab as plt
import numpy as np
import os
from os import path
import pprint
import random
import shutil
import sys
from sys import stdout
import theano
from theano import tensor as T
from theano.sandbox.rng_mrg import MRG_RandomStreams as RandomStreams
import time
import yaml

from GSN import likelihood_estimation_parzen as lep
from gru import GenGRU
from rbm import RBM
from rnn import GenRNN
from horses import HorsesPieces
from horses import SimpleHorses
from layers import Averager
from layers import BaselineWithInput
from layers import MLP
from layers import ParzenEstimator
from mnist import mnist_iterator
from mnist import MNIST_Pieces
import op
import tools
from tools import check_bad_nums
from tools import itemlist
from tools import load_model
from tools import log_mean_exp
from tools import parzen_estimation


floatX = theano.config.floatX


def unpack(mode=None,
           dim_h=None,
           h_init=None,
           mlp_a=None, mlp_b=None, mlp_o=None, mlp_c=None,
           dataset='horses',
           dataset_args=None,
           **model_args):

    dataset_args = dataset_args[()]

    if mlp_a is not None:
        mlp_a = mlp_a[()]
    if mlp_b is not None:
        mlp_b = mlp_b[()]
    if mlp_o is not None:
        mlp_o = mlp_o[()]
    if mlp_c is not None:
        mlp_c = mlp_c[()]

    trng = RandomStreams(random.randint(0, 100000))

    if dataset == 'mnist':
        dim_in = 28 * 28
    elif dataset == 'horses':
        dims = dataset_args['dims']
        dim_in = dims[0] * dims[1]
    else:
        raise ValueError()

    def load_mlp(name, dim_in, dim_out,
                 dim_h=None, n_layers=None,
                 **kwargs):
        out_act = 'T.tanh'
        mlp = MLP(dim_in, dim_h, dim_out, n_layers, name=name, **kwargs)
        return mlp

    if mlp_a is not None:
        MLPa = load_mlp('MLPa', dim_in, 2 * dim_h, **mlp_a)
    else:
        MLPa = None
    if mlp_b is not None:
        MLPb = load_mlp('MLPb', dim_in, dim_h, **mlp_b)
    else:
        MLPb = None
    if mlp_o is not None:
        MLPo = load_mlp('MLPo', dim_h, dim_in, **mlp_o)
    else:
        MLPo = None
    if mlp_c is not None:
        MLPc = load_mlp('MLPc', dim_in, dim_in, **mlp_c)
    else:
        MLPc = None

    if mode == 'rnn':
        MLPa = MLPb
        MLPb = None

    if mode == 'gru':
        rnn = GenGRU(dim_in, dim_h, MLPa=MLPa, MLPb=MLPb, MLPo=MLPo, MLPc=MLPc)
        models = [rnn, rnn.MLPa, rnn.MLPb, rnn.MLPo]
    elif mode == 'rnn':
        rnn = GenRNN(dim_in, dim_h, MLPa=MLPa, MLPo=MLPo, MLPc=MLPc)
        models = [rnn, rnn.MLPa, rnn.MLPo]
    else:
        raise ValueError('Mode %s not recognized' % mode)

    if mlp_c is not None:
        models.append(rnn.MLPc)

    if h_init == 'average':
        averager = Averager((batch_size, dim_h))
        models.append(averager)
    elif h_init == 'mlp':
        mlp = MLP(dim_in, dim_h, dim_h, 1, out_act='T.tanh', name='MLPh')
        models.append(mlp)

    return models, model_args, dict(
        mode=mode,
        h_init=h_init,
        dataset=dataset,
        dataset_args=dataset_args
    )

def load_model_for_sampling(model_file):
    models, kwargs = load_model(model_file, unpack)
    dataset_args = kwargs['dataset_args']
    dataset = kwargs['dataset']

    if dataset == 'mnist':
        train = MNIST_Chains(batch_size=1, mode='train', **dataset_args)
        test = MNIST_Chains(batch_size=1, mode='test', **dataset_args)
    elif dataset == 'horses':
        train = Horses(batch_size=1, crop_image=True, **dataset_args)
    else:
        raise ValueError()

    rnn = models['gen_{mode}'.format(mode=kwargs['mode'])]

    h_init = kwargs['h_init']
    if h_init == 'average':
        averager = models['averager']
        h0 = averager.params['m']
        f_h0 = lambda x: h0
    elif h_init == 'mlp':
        X0 = T.matrix('x0', dtype=floatX)
        mlp = models['MLPh']
        mlp.set_tparams()
        f_init = theano.function([X0], mlp(X0))
        f_h0 = lambda x: f_init(x)
    else:
        h0 = np.zeros((1, rnn.dim_h)).astype('float32')
        f_h0 = lambda x: h0

    tparams = rnn.set_tparams()
    train.set_f_energy(energy_function, rnn)

    return rnn, train, test, f_h0

def get_sample_cross_correlation(model_file, n_steps=100):
    rnn, dataset, test, f_h0 = load_model_for_sampling(model_file)
    samples = generate_samples(model_file, n_steps=n_steps, n_samples=1)[1:, 0]

    c = np.corrcoef(samples, samples)[:n_steps, n_steps:]
    plt.imshow(c)
    plt.colorbar()
    plt.show()

def generate(model_file, n_steps=20, n_samples=40, out_path=None):
    rnn, train, test, f_h0 = load_model_for_sampling(model_file)
    params = rnn.get_sample_params()

    X = T.matrix('x', dtype=floatX)
    H = T.matrix('h', dtype=floatX)
    h_s, x_s, p_s = rnn.step_sample(H, X, *params)
    f_sam = theano.function([X, H], [x_s, h_s, p_s])

    x = rnn.rng.binomial(p=0.5, size=(n_samples, rnn.dim_in), n=1).astype(floatX)
    ps = [x]
    h = f_h0(x)
    for s in xrange(n_steps):
        x, h, p = f_sam(x, h)
        ps.append(p)

    if out_path is not None:
        train.save_images(np.array(ps), path.join(out_path, 'generation_samples.png'))
    else:
        return ps[-1]

def generate_samples(model_file, n_steps=1000, n_samples=1):
    rnn, dataset, test, f_h0 = load_model_for_sampling(model_file)

    X = T.matrix('x', dtype=floatX)
    H = T.matrix('h', dtype=floatX)

    out_s, updates_s = rnn.sample(x0=X, h0=H, n_steps=n_steps)
    f_sample = theano.function([X, H], out_s['p'], updates=updates_s)

    x = dataset.next_simple(batch_size=n_samples)
    h = f_h0(x)
    sample_chain = f_sample(x, h)
    return sample_chain

def visualize(model_file, out_path=None, interval=1, n_samples=-1,
              save_movie=True, use_data_every=50, use_data_in=False,
              save_hiddens=False):
    rnn, train, test, f_h0 = load_model_for_sampling(model_file)
    params = rnn.get_sample_params()

    X = T.matrix('x', dtype=floatX)
    H = T.matrix('h', dtype=floatX)
    h_s, x_s, p_s = rnn.step_sample(H, X, *params)
    f_sam = theano.function([X, H], [x_s, h_s, p_s])
    ps = []
    xs = []
    hs = []

    try:
        x = train.X[:1]
        h = f_h0(x)
        s = 0
        while True:
            stdout.write('\rSampling (%d): Press ^c to stop' % s)
            stdout.flush()
            x, h, p = f_sam(x, h)
            hs.append(h)
            xs.append(x)
            if use_data_every > 0 and s % use_data_every == 0:
                x_n = train.next_simple(20)
                energies, _, h_p = train.f_energy(x_n, x, h)
                energies = energies[0]
                x = x_n[np.argmin(energies)][None, :]
                if use_data_in:
                    ps.append(x)
                else:
                    ps.append(p)
            else:
                ps.append(p)

            s += 1
            if n_samples != -1 and s > n_samples:
                raise KeyboardInterrupt()
    except KeyboardInterrupt:
        print 'Finishing'

    if out_path is not None:
        train.save_images(np.array(ps), path.join(out_path, 'vis_samples.png'), x_limit=100)
        if save_hiddens:
            np.save(path.join(out_path, 'hiddens.npy'), np.array(hs))

    fig = plt.figure()
    data = np.zeros(train.dims)
    im = plt.imshow(data, vmin=0, vmax=1, cmap='Greys_r')

    def init():
        im.set_data(np.zeros(train.dims))

    def animate(i):
        data = ps[i].reshape(train.dims)
        im.set_data(data)
        return im

    anim = animation.FuncAnimation(fig, animate, init_func=init, frames=s,
                                   interval=interval)

    if out_path is not None and save_movie:
        print 'Saving movie'
        Writer = animation.writers['ffmpeg']
        writer = Writer(fps=15, metadata=dict(artist='Devon Hjelm'), bitrate=1800)
        anim.save(path.join(out_path, 'vis_movie.mp4'), writer=writer)
    else:
        print 'Showing movie'
        plt.show()

    if out_path is not None and save_movie:
        train.next()
        fig = plt.figure()
        data = np.zeros(train.dims)
        X_tr = train._load_chains()
        im = plt.imshow(data, vmin=0, vmax=1, cmap='Greys_r')

        def animate_training_examples(i):
            data = X_tr[i, 0].reshape(train.dims)
            im.set_data(data)
            return im

        def init():
            im.set_data(np.zeros(train.dims))

        anim = animation.FuncAnimation(fig, animate_training_examples,
                                       init_func=init, frames=X_tr.shape[0],
                                       interval=interval)

        print 'Saving data movie'
        Writer = animation.writers['ffmpeg']
        writer = Writer(fps=15, metadata=dict(artist='Devon Hjelm'), bitrate=1800)
        anim.save(path.join(out_path, 'vis_train_movie.mp4'), writer=writer)

def energy_function(model):
    x = T.tensor3('x', dtype=floatX)
    x_p = T.matrix('x_p', dtype=floatX)
    h_p = T.matrix('h_p', dtype=floatX)

    params = model.get_sample_params()
    h, x_s, p = model.step_sample(h_p, x_p, *params)

    p = T.alloc(0., p.shape[0], x.shape[0], x.shape[1]).astype(floatX) + p[:, None, :]

    energy = -(x * T.log(p + 1e-7) + (1 - x) * T.log(1 - p + 1e-7)).sum(axis=2)

    return theano.function([x, x_p, h_p], [energy, x_s, h])

def euclidean_distance(model):
    '''
    h_p are dummy variables to keep it working for dataset chain generators.
    '''

    x = T.tensor3('x', dtype=floatX)
    x_p = T.matrix('x_p', dtype=floatX)
    h_p = T.matrix('h_p', dtype=floatX)
    x_pe = T.alloc(0., x_p.shape[0], x.shape[0], x_p.shape[1]).astype(floatX) + x_p[:, None, :]

    params = model.get_sample_params()
    distance = (x - x_pe) ** 2
    distance = distance.sum(axis=2)
    return theano.function([x, x_p, h_p], [distance, x, h_p])

def train_model(save_graphs=False, out_path='', name='',
                load_last=False, model_to_load=None, save_images=True,
                source=None,
                learning_rate=0.01, optimizer='adam', batch_size=10, steps=1000,
                mode='gru',
                metric='energy',
                dim_h=500,
                mlp_a=None, mlp_b=None, mlp_o=None, mlp_c=None,
                dataset=None, dataset_args=None,
                noise_input=True, sample=True,
                h_init='mlp',
                model_save_freq=100, show_freq=10):

    print 'Dataset args: %s' % pprint.pformat(dataset_args)
    window = dataset_args['window']
    stride = min(window, dataset_args['chain_stride'])
    out_path = path.abspath(out_path)

    if dataset == 'mnist':
        train = MNIST_Pieces(batch_size=batch_size, out_path=out_path, **dataset_args)
    elif dataset == 'horses':
        raise NotImplementedError()
        train = Horses_Pieces(batch_size=batch_size, out_path=out_path, crop_image=True, **dataset_args)
    else:
        raise ValueError()

    dim_in = train.dim
    X = T.tensor3('x', dtype=floatX)
    Y = T.matrix('y', dtype=floatX)
    trng = RandomStreams(random.randint(0, 100000))

    if mode == 'gru':
        C = GenGRU
    elif mode == 'rnn':
        C = GenRNN
    else:
        raise ValueError()

    print 'Forming model'

    def load_mlp(name, dim_in, dim_out,
                 dim_h=None, n_layers=None,
                 **kwargs):
        out_act = 'T.tanh'
        mlp = MLP(dim_in, dim_h, dim_out, n_layers, **kwargs)
        return mlp

    if model_to_load is not None:
        models, _ = load_model(model_to_load, unpack)
    elif load_last:
        model_file = glob(path.join(out_path, '*last.npz'))[0]
        models, _ = load_model(model_file, unpack)
    else:
        mlps = {}
        if mode == 'gru':
            if mlp_a is not None:
                MLPa = load_mlp('MLPa', dim_in, 2 * dim_h, **mlp_a)
            else:
                MLPa = None
            mlps['MLPa'] = MLPa

        if mlp_b is not None:
            MLPb = load_mlp('MLPb', dim_in, dim_h, **mlp_b)
        else:
            MLPb = None
        if mode == 'gru':
            mlps['MLPb'] = MLPb
        else:
            mlps['MLPa'] = MLPb

        if mlp_o is not None:
            MLPo = load_mlp('MLPo', dim_h, dim_in, **mlp_o)
        else:
            MLPo = None
        mlps['MLPo'] = MLPo

        if mlp_c is not None:
            MLPc = load_mlp('MLPc', dim_in, dim_in, **mlp_c)
        else:
            MLPc = None
        mlps['MLPc'] = MLPc

        rnn = C(dim_in, dim_h, trng=trng,
                **mlps)
        models = OrderedDict()
        models[rnn.name] = rnn

    print 'Getting params...'
    rnn = models['gen_{mode}'.format(mode=mode)]
    tparams = rnn.set_tparams()

    X = trng.binomial(p=X, size=X.shape, n=1, dtype=X.dtype)
    X_s = X[:-1]
    updates = theano.OrderedUpdates()
    if noise_input:
        X_s = X_s * (1 - trng.binomial(p=0.1, size=X_s.shape, n=1, dtype=X_s.dtype))

    if h_init is None:
        h0 = None
    elif h_init == 'last':
        print 'Initializing h0 from chain'
        h0 = theano.shared(np.zeros((batch_size, rnn.dim_h)).astype(floatX))
    elif h_init == 'average':
        print 'Initializing h0 from running average'
        if 'averager' in models.keys():
            'Found pretrained averager'
            averager = models['averager']
        else:
            averager = Averager((dim_h))
        tparams.update(averager.set_tparams())
        h0 = (T.alloc(0., batch_size, rnn.dim_h) + averager.m[None, :]).astype(floatX)
    elif h_init == 'mlp':
        print 'Initializing h0 from MLP'
        if 'MLPh' in models.keys():
            print 'Found pretrained MLP'
            mlp = models['MLPh']
        else:
            mlp = MLP(rnn.dim_in, rnn.dim_h, rnn.dim_h, 1,
                      out_act='T.tanh',
                      name='MLPh')
        tparams.update(mlp.set_tparams())
        h0 = mlp(X[0])

    print 'Model params: %s' % tparams.keys()
    if metric == 'energy':
        print 'Energy-based metric'
        train.set_f_energy(energy_function, rnn)
    elif metric in ['euclidean', 'euclidean_then_energy']:
        print 'Euclidean-based metic'
        train.set_f_energy(euclidean_distance, rnn)
    else:
        raise ValueError(metric)

    outs, updates_1 = rnn(X_s, h0=h0)
    h = outs['h']
    p = outs['p']
    x = outs['y']
    updates.update(updates_1)

    energy = -(X[1:] * T.log(p + 1e-7) + (1 - X[1:]) * T.log(1 - p + 1e-7)).sum(axis=(0, 2))
    cost = energy.mean()
    consider_constant = [x]

    if h_init == 'last':
        updates += [(h0, h[stride - 1])]
    elif h_init == 'average':
        outs_h, updates_h = averager(h)
        updates.update(updates_h)
    elif h_init == 'mlp':
        h_c = T.zeros_like(h[0]) + h[0]
        cost += ((h0 - h[0])**2).sum(axis=1).mean()
        consider_constant.append(h_c)

    extra_outs = [energy.mean(), h, p]

    if sample:
        print 'Setting up sampler'
        if h_init == 'average':
            h0_s = T.alloc(0., window, rnn.dim_h).astype(floatX) + averager.m[None, :]
        elif h_init == 'mlp':
            h0_s = mlp(X[:, 0])
        elif h_init == 'last':
            h0_s = h[:, 0]
        else:
            h0_s = None
        out_s, updates_s = rnn.sample(X[:, 0], h0=h0_s, n_samples=10, n_steps=10)
        f_sample = theano.function([X], out_s['p'], updates=updates_s)

    grad_tparams = OrderedDict((k, v)
        for k, v in tparams.iteritems()
        if (v not in updates.keys()))

    grads = T.grad(cost, wrt=itemlist(grad_tparams),
                   consider_constant=consider_constant)

    print 'Building optimizer'
    lr = T.scalar(name='lr')
    f_grad_shared, f_grad_updates = eval('op.' + optimizer)(
        lr, tparams, grads, [X], cost,
        extra_ups=updates,
        extra_outs=extra_outs)

    print 'Actually running'

    try:
        e = 0
        for s in xrange(steps):
            try:
                x, _ = train.next()
            except StopIteration:
                e += 1
                print 'Epoch {epoch}'.format(epoch=e)
                if metric == 'euclidean_then_energy' and e == 2:
                    print 'Switching to model energy'
                    train.set_f_energy(energy_function, rnn)
                continue
            rval = f_grad_shared(x)

            if check_bad_nums(rval, ['cost', 'energy', 'h', 'x', 'p']):
                return

            if s % show_freq == 0:
                print ('%d: cost: %.5f | energy: %.2f | prob: %.2f'
                       % (e, rval[0], rval[1], np.exp(-rval[1])))
            if s % show_freq == 0:
                idx = np.random.randint(rval[3].shape[1])
                samples = np.concatenate([x[1:, idx, :][None, :, :],
                                        rval[3][:, idx, :][None, :, :]], axis=0)
                train.save_images(
                    samples,
                    path.join(
                        out_path,
                        '{name}_inference_chain.png'.format(name=name)))
                train.save_images(
                    x, path.join(
                        out_path, '{name}_input_samples.png'.format(name=name)))
                if sample:
                    sample_chain = f_sample(x)
                    train.save_images(
                        sample_chain,
                        path.join(
                            out_path, '{name}_samples.png'.format(name=name)))
            if s % model_save_freq == 0:
                temp_file = path.join(
                    out_path, '{name}_temp.npz'.format(name=name))
                d = dict((k, v.get_value()) for k, v in tparams.items())
                d.update(mode=mode,
                         dim_h=dim_h,
                         h_init=h_init,
                         mlp_a=mlp_a, mlp_b=mlp_b, mlp_o=mlp_o, mlp_c=mlp_c,
                         dataset=dataset, dataset_args=dataset_args)
                np.savez(temp_file, **d)

            f_grad_updates(learning_rate)
    except KeyboardInterrupt:
        print 'Training interrupted'

    outfile = os.path.join(
        out_path, '{name}_{t}.npz'.format(name=name, t=int(time.time())))
    last_outfile = path.join(out_path, '{name}_last.npz'.format(name=name))

    print 'Saving the following params: %s' % tparams.keys()
    d = dict((k, v.get_value()) for k, v in tparams.items())
    d.update(mode=mode,
             dim_h=dim_h,
             h_init=h_init,
             mlp_a=mlp_a, mlp_b=mlp_b, mlp_o=mlp_o, mlp_c=mlp_c,
             dataset=dataset, dataset_args=dataset_args)

    np.savez(outfile, **d)
    np.savez(last_outfile,  **d)
    print 'Done saving. Bye bye.'

def make_argument_parser():
    parser = argparse.ArgumentParser()
    parser.add_argument('experiment', default=None)
    parser.add_argument('-o', '--out_path', default=None,
                        help='Output path for stuff')
    parser.add_argument('-l', '--load_last', action='store_true')
    parser.add_argument('-r', '--load_model', default=None)
    parser.add_argument('-i', '--save_images', action='store_true')
    return parser

def load_experiment(experiment_yaml):
    print('Loading experiment from %s' % experiment_yaml)
    exp_dict = yaml.load(open(experiment_yaml))
    print('Experiment hyperparams: %s' % pprint.pformat(exp_dict))
    return exp_dict

if __name__ == '__main__':
    parser = make_argument_parser()
    args = parser.parse_args()

    exp_dict = load_experiment(path.abspath(args.experiment))
    out_path = path.join(args.out_path, exp_dict['name'])

    if out_path is not None:
        if path.isfile(out_path):
            raise ValueError()
        elif not path.isdir(out_path):
            os.mkdir(path.abspath(out_path))

    shutil.copy(path.abspath(args.experiment), path.abspath(out_path))

    train_model(out_path=out_path, load_last=args.load_last,
                model_to_load=args.load_model, save_images=args.save_images,
                **exp_dict)
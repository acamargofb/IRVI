import cPickle
import gzip
import multiprocessing as mp
import numpy as np
from os import path
import PIL
from progressbar import ProgressBar
import random
import sys
from sys import stdout
import theano
from theano import tensor as T
import time
import traceback

from utils.tools import (
    concatenate,
    init_rngs,
    rng_,
    scan
)
from utils.vis_utils import tile_raster_images


def get_iter(inf=False, batch_size=128):
    return mnist_iterator(inf=inf, batch_size=batch_size)

class MNIST(object):
    def __init__(self, batch_size=128, source=None,
                 restrict_digits=None, mode='train', shuffle=True, inf=False,
                 binarize=False,
                 stop=None, out_path=None):
        print 'Loading MNIST ({mode})'.format(mode=mode)

        with gzip.open(source, 'rb') as f:
            x = cPickle.load(f)

        X, Y = self.get_data(x, mode)

        self.dims = (28, 28)
        self.out_path = out_path

        if restrict_digits is None:
            n_classes = 10
        else:
            n_classes = len(restrict_digits)

        O = np.zeros((X.shape[0], n_classes), dtype='float32')

        if restrict_digits is None:
            for idx in xrange(X.shape[0]):
                O[idx, Y[idx]] = 1.;
        else:
            print 'Restricting to digits %s' % restrict_digits
            new_X = []
            i = 0
            for j in xrange(X.shape[0]):
                if Y[j] in restrict_digits:
                    new_X.append(X[j])
                    c_idx = restrict_digits.index(Y[j])
                    O[i, c_idx] = 1.;
                    i += 1
            X = np.float32(new_X)

        if stop is not None:
            X = X[:stop]

        self.n, self.dim = X.shape
        print 'Data shape: %d x %d' % X.shape

        self.shuffle = shuffle
        self.pos = 0
        self.bs = batch_size
        self.inf = inf
        self.next = self._next

        if binarize:
            print 'Binarizing MNIST'
            X = rng_.binomial(p=X, size=X.shape, n=1).astype('float32')

        self.X = X
        self.O = O

        self.mean_image = self.X.mean(axis=0)

        if self.shuffle:
            self.randomize()

    def get_data(self, x, mode):
        if mode == 'train':
            X = np.float32(x[0][0])
            Y = np.float32(x[0][1])
        elif mode == 'valid':
            X = np.float32(x[1][0])
            Y = np.float32(x[1][1])
        elif mode == 'test':
            X = np.float32(x[2][0])
            Y = np.float32(x[2][1])
        else:
            raise ValueError()

        return X, Y

    def __iter__(self):
        return self

    def randomize(self):
        rnd_idx = np.random.permutation(np.arange(0, self.n, 1))
        self.X = self.X[rnd_idx, :]
        self.O = self.O[rnd_idx, :]

    def next(self):
        raise NotImplementedError()

    def reset(self):
        self.pos = 0
        if self.shuffle:
            self.randomize()

    def _next(self, batch_size=None):
        if batch_size is None:
            batch_size = self.bs

        if self.pos == -1:
            self.reset()

            if not self.inf:
                raise StopIteration

        x = self.X[self.pos:self.pos+batch_size]
        y = self.O[self.pos:self.pos+batch_size]

        self.pos += batch_size
        if self.pos + batch_size > self.n:
            self.pos = -1

        return x, y

    def save_images(self, x, imgfile, transpose=False, x_limit=None):
        if len(x.shape) == 2:
            x = x.reshape((x.shape[0], 1, x.shape[1]))

        if x_limit is not None and x.shape[0] > x_limit:
            x = np.concatenate([x, np.zeros((x_limit - x.shape[0] % x_limit,
                                             x.shape[1],
                                             x.shape[2])).astype('float32')],
                axis=0)
            x = x.reshape((x_limit, x.shape[0] * x.shape[1] // x_limit, x.shape[2]))

        tshape = x.shape[0], x.shape[1]
        x = x.reshape((x.shape[0] * x.shape[1], x.shape[2]))
        image = self.show(x.T, tshape, transpose=transpose)
        image.save(imgfile)

    def show(self, image, tshape, transpose=False):
        fshape = self.dims
        if transpose:
            X = image
        else:
            X = image.T

        return PIL.Image.fromarray(tile_raster_images(
            X=X, img_shape=fshape, tile_shape=tshape,
            tile_spacing=(1, 1)))

    def translate(self, x):
        return x


class Chains(object):
    def __init__(self, D, batch_size=10,
                 window=20, chain_length=5000, build_batch=1000,
                 chain_stride=None, n_chains=1,
                 use_theano=False,
                 trim_end=0, out_path=None, **kwargs):

        self.dataset = D(**kwargs)

        self.batch_size = batch_size
        self.f_energy = None
        self.f_chain = None
        self.window = window
        self.build_batch = build_batch
        self.trim_end = trim_end
        self.chain_length = min(chain_length, self.dataset.n)
        self.dim_h = None
        self.out_path = out_path
        self.use_theano = use_theano

        if chain_stride is None:
            self.chain_stride = self.window
        else:
            self.chain_stride = chain_stride

        self.next = self._next
        self.cpos = -1
        init_rngs(self, **kwargs)

    def next(self):
        raise NotImplementedError()

    def set_f_energy(self, f_energy, dim_h, model=None):
        self.dim_h = dim_h

        # Energy function -----------------------------------------------------
        X = T.matrix('x', dtype='float32')
        x_p = T.vector('x_p', dtype='float32')
        h_p = T.vector('h_p', dtype='float32')

        energy, x_n, h_n = f_energy(X, x_p, h_p, model)
        self.f_energy = theano.function([X, x_p, h_p], [energy, x_n, h_n])

        # Chain function ------------------------------------------------------
        counts = T.ones((X.shape[0],)).astype('int64')
        P = T.scalar('P', dtype='int64')
        x_p = X[0]
        h_p = self.trng.normal(avg=0., std=1., size=(dim_h,)).astype('float32')
        counts = T.set_subtensor(counts[0], 0)

        def step(i, x_p, h_p, counts, x):
            energies, _, h_n = f_energy(x, x_p, h_p, model)
            energies = energies / counts
            i = T.argmin(energies)
            counts = T.set_subtensor(counts[i], 0)
            return i, x[i], h_n, counts

        seqs = []
        outputs_info = [T.constant(0).astype('int64'), x_p, h_p, counts]
        non_seqs = [X]

        (chain, x_chain, h_chain, counts), updates = scan(
            step, seqs, outputs_info, non_seqs, X.shape[0] - 1, name='make_chain',
            strict=False)

        chain += P
        chain_e = T.zeros((chain.shape[0] + 1,)).astype('int64')
        chain = T.set_subtensor(chain_e[1:], chain)
        self.f_chain = theano.function([X, P], chain, updates=updates)

    def _build_chain_py(self, x, data_pos):
        h_p = np.random.normal(
            loc=0, scale=1, size=(self.dim_h,)).astype('float32')

        l_chain = x.shape[0]
        n_samples = min(self.build_batch, l_chain)
        chain_idx = range(l_chain)
        rnd_idx = np.random.permutation(np.arange(0, l_chain, 1))
        chain_idx = [chain_idx[i] for i in rnd_idx]

        counts = [True for _ in xrange(l_chain)]
        n = l_chain
        chain = []

        pbar = ProgressBar(maxval=l_chain).start()
        while n > 0:
            idx = [j for j in chain_idx if counts[j]]
            x_idx = [idx[i] for i in range(min(n_samples, n))]
            assert len(np.unique(x_idx)) == len(x_idx)

            if n == l_chain:
                picked_idx = random.choice(x_idx)
            else:
                assert x_p is not None
                x_n = x[x_idx]
                energies, _, h_p = self.f_energy(x_n, x_p, h_p)
                i = np.argmin(energies)
                picked_idx = x_idx[i]

            counts[picked_idx] = False
            assert not (picked_idx + data_pos) in self.chain
            chain.append(picked_idx + data_pos)

            x_p = x[picked_idx]
            n -= 1

            nd_idx = np.random.permutation(np.arange(0, l_chain, 1))
            chain_idx = [chain_idx[i] for i in rnd_idx]

            pbar.update(l_chain - n)

        return chain

    def _build_chain(self, trim_end=0):
        self.chain = []
        n_remaining_samples = self.dataset.n - self.dataset.pos
        l_chain = min(self.chain_length, n_remaining_samples)

        data_pos = self.dataset.pos
        x, _ = self.dataset.next(batch_size=l_chain)
        n_samples = min(self.build_batch, l_chain)

        t0 = time.time()
        if self.use_theano:
            print('Resetting chain with length %d using all datapoints (theano). '
                  'Position in data is %d'
                  % (l_chain, data_pos))
            self.chain = self.f_chain(x, data_pos)
        else:
            print('Resetting chain with length %d and %d samples per query. '
                  'Position in data is %d'
                  % (l_chain, n_samples, data_pos))
            self.chain = self._build_chain_py(x, data_pos)
        t1 = time.time()
        print 'Chain took %.2f seconds' % (t1 - t0)

        if self.out_path is not None:
            self.save_images(self._load_chains(),
                             path.join(self.out_path,
                                       '%s_chain_%d.png' % (self.mode, self.pos)),
                             x_limit=200)

        if trim_end:
            print 'Trimming %d' % trim_end
            self.chain = self.chain[:-trim_end]

        if self.out_path is not None:
            self.save_images(
                self._load_chains(),
                path.join(self.out_path,
                          '%s_chain_%d_trimmed.png' % (self.mode, self.pos)),
                x_limit=200)

    def _load_chains(self, chains=None):
        if chains is None:
            chains = [self.chain]

        x = np.zeros((len(chains[0]), len(chains), self.dataset.dim)).astype('float32')
        for i, c in enumerate(chains):
            x[:, i] = self.dataset.X[c]
        return x

    def reset(self):
        self.dataset.reset()
        self.cpos = -1

    def _next(self, l_chain=None):
        assert self.f_energy is not None

        chain_length = min(self.chain_length - self.trim_end,
                           self.dataset.n - self.dataset.pos - self.trim_end)
        window = min(self.window, chain_length)

        if self.cpos == -1:
            self.cpos = 0
            self._build_chain(trim_end=self.trim_end)
            self.chain_idx = range(0, chain_length - window + 1, self.chain_stride)
            random.shuffle(self.chain_idx)

        chains = []
        for b in xrange(self.batch_size):
            p = self.chain_idx[b + self.cpos]
            chains.append([self.chain[j] for j in xrange(p, p + window)])

        x = self._load_chains(chains=chains)

        if self.cpos + 2 * self.batch_size >= len(self.chain_idx):
            self.cpos = -1
        else:
            self.cpos += self.batch_size

        return x

    def next_simple(self, batch_size=None):
        x, y = super(MNIST_Chain, self)._next(batch_size=batch_size)
        return x, y


class MNIST_Pieces(MNIST):
    def __init__(self, batch_size=1, source='/Users/devon/Data/mnist.pkl.gz',
                 restrict_digits=None, mode='train', shuffle=True,
                 width=5, stride=5,
                 stop=None, out_path=None, chain_stride=None):
        with gzip.open(source, 'rb') as f:
            x = cPickle.load(f)

        X, Y = self.get_data(x, mode)

        self.dims = (28, 28)
        self.f_energy = None
        self.stride = stride
        self.bs = batch_size

        self.out_path = out_path
        if chain_stride is None:
            self.chain_stride = self.chain_length
        else:
            self.chain_stride = chain_stride

        self.shuffle = shuffle

        X, O = self.process(X, Y, restrict_digits)

        if stop is not None:
            X = X[:stop]

        Y = self.split(X. self.width, self.stride)

        self.n, self.chain_length, self.dim = Y.shape
        self.chains = [[] for _ in xrange(self.bs)]
        self.pos = 0
        self.spos = 0

        self.X = X
        self.Y = Y
        self.O = O

        if self.shuffle:
            print 'Shuffling mnist'
            self.randomize()

    def split(self, X, width, stride):
        step_x = range(0, self.dims[0] - width, stride)
        step_y = range(0, self.dims[1] - width, stride)
        n_windows = len(step_x) * len(step_y)
        Y = np.zeros((X.shape[0], n_windows, width ** 2))
        for i, S in enumerate(X):
            for x in step_x:
                for y in step_y:
                    p = x + y * width
                    idx = [range(p + w * self.dims[0],
                                 p + w * self.dims[0] + self.width)
                           for w in range(width)]
                    idx = [i for s in idx for i in s]
                    Y[i, p] = S[idx]

        return Y

    def randomize(self):
        rnd_idx = np.random.permutation(np.arange(0, self.n, 1))
        self.X = self.X[rnd_idx, :]
        self.O = self.O[rnd_idx, :]
        self.Y = self.Y[rnd_idx, :, :]

    def _load_chains(self, chains=None):
        if chains is None:
            chains = self.chains

        l_chains = len(chains[0])
        n_chains = len(chains)
        x = np.zeros((l_chains, n_chains, self.dim)).astype('float32')
        for i, (c, ps) in enumerate(chains):
            x[:, i] = self.Y[c + self.pos, ps]
        return x

    def _build_chain(self, x_p=None, h_p=None):
        n_chains = len(self.chains)
        l_chain = self.chain_length
        n_samples = l_chain
        chain_idx = [range(l_chain) for _ in xrange(n_chains)]

        print('Resetting chains with length %d and %d samples per query.'
              % (l_chain, n_samples))

        for c in xrange(n_chains):
            rnd_idx = np.random.permutation(np.arange(0, l_chain, 1))
            chain_idx[c] = [chain_idx[c][i] for i in rnd_idx]

        counts = [[True for _ in xrange(l_chain)] for _ in xrange(n_chains)]
        n = l_chain

        while n > 0:
            stdout.write('\r%d         ' % n); stdout.flush()
            x_idx = []

            for c in xrange(n_chains):
                idx = [j for j in chain_idx[c] if counts[c][j]]
                x_idx.append(idx)

            x = np.array([self.Y[c + self.pos, js] for c, js in enumerate(x_idx)]).astype(floatX)

            if n == l_chain:
                picked_idx = [random.choice(idx) for idx in x_idx]
            else:
                assert x_p is not None
                energies, _, h_p = self.f_energy(x, x_p, h_p)
                picked_idx = []
                for c, js in enumerate(x_idx):
                    chain_energies = energies[c]
                    i = np.argmin(chain_energies)
                    j = js[i]
                    picked_idx.append(j)

            for c in xrange(n_chains):
                j = picked_idx[c]
                counts[c][j] = False
                self.chains[c].append(j)

            x_p = np.array([self.Y[c, j]
                            for c, j
                            in enumerate(picked_idx)]).astype(floatX)
            n -= 1

    def next(self, save_chain=False):
        assert self.f_energy is not None

        if self.pos == 0:
            self.randomize()

        batch_size = min(self.bs, self.n - self.pos)
        self.chains = [[] for _ in xrange(batch_size)]

        h_p = np.random.normal(loc=0, scale=1, size=(batch_size, self.dim_h)).astype('float32')
        self._build_chain(x_p=None, h_p=h_p)

        assert len(np.unique(self.chains[0])) == len(self.chains[0]), (len(np.unique(self.chains[0])), len(self.chains[0]))
        for i in self.chains[0]:
            assert i >= self.pos and i < self.pos + self.chain_length

        if self.out_path and save_chain:
            self.save_images(
                self._load_chains(),
                path.join(self.out_path, 'training_chain_%d.png' % self.pos),
                x_limit=200)

        x = self._load_chains()
        ps = [[p for p in chain] for chain in self.chains]
        coords = np.array([[p % self.width, p // self.width] for p in ps]).astype(floatX)

        self.pos += self.bs
        if self.pos >= self.n:
            self.pos = 0
            self.randomize()

        return x, coords

    def next_simple(self, batch_size=10):
        cpos = self.spos
        if cpos + batch_size > self.n:
            self.spos = 0
            cpos = self.spos
            if self.shuffle:
                self.randomize()

        x = self.X[cpos:cpos+batch_size]
        self.spos += batch_size

        return x

    def draw(self, ps):
        pallet = T.alloc(
            0., ps.shape[0], ps.shape[1],
            self.dims[0] * self.dims[1]
            ).astype(floatX)

        def step_draw(p, pallet):
            x = p[:, :-4]
            pos = p[:, -4:-2]
            x_pos = floor(pos[0])
            y_pos = floor(pos[1])

        seqs = [ps]
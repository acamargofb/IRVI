'''
Iterative refinement of the approximate posterior
'''

from collections import OrderedDict
import theano
from theano import tensor as T

from utils import floatX
from utils.tools import (
    scan,
    update_dict_of_lists,
    warn_kwargs
)

class IRVI(object):

    def __init__(self,
                 model,
                 name='IRVI',
                 inference_rate=0.1,
                 n_inference_samples=20,
                 n_inference_steps=20,
                 pass_gradients=True,
                 init_inference='recognition_network',
                 **kwargs):

        self.name = name
        self.model = model
        self.init_inference = init_inference
        self.inference_rate = inference_rate
        self.n_inference_steps = n_inference_steps
        self.n_inference_samples = n_inference_samples
        self.pass_gradients = pass_gradients
        warn_kwargs(self, **kwargs)

    def step_infer(self, *params):  raise NotImplementedError()
    def init_infer(self, q):        raise NotImplementedError()
    def unpack_infer(self, outs):   raise NotImplementedError()
    def params_infer(self):         raise NotImplementedError()

    def init_variational_inference(self, x):
        model = self.model

        if self.init_inference == 'recognition_network':
            print 'Initializing %s inference with recognition network' % self.name
            q0 = model.posterior.feed(x)
        elif self.init_inference == 'from_prior':
            print 'Initializing %s inference with prior parameters' % self.name
            q0 = model.prior.get_center(**model.prior.get_params())
        else:
            raise ValueError(self.init_inference)

        return q0

    def inference(self, x, y, q0=None):

        model = self.model
        updates = theano.OrderedUpdates()

        if q0 is None:
            q0 = self.init_variational_inference(x)

        epsilons = model.posterior.distribution.prototype_samples(
            (self.n_inference_steps, self.n_inference_samples,
             x.shape[0], model.dim_h))

        seqs = [epsilons]
        outputs_info = [q0] + self.init_infer(q0) + [None]
        non_seqs = [y] + self.params_infer() + model.get_params()

        print ('Doing %d inference steps of %s and a rate of %.5f with %d '
               'inference samples'
               % (self.n_inference_steps, self.name,
                  self.inference_rate, self.n_inference_samples))

        if self.n_inference_steps > 1:
            print 'Multiple inference steps. Using `scan`'
            outs, updates_i = scan(
                self.step_infer, seqs, outputs_info, non_seqs, self.n_inference_steps,
                self.name + '_infer'
            )
            updates.update(updates_i)
            qs, i_costs = self.unpack_infer(outs)
            qs = T.concatenate([q0[None, :, :], qs], axis=0)

        elif self.n_inference_steps == 1:
            print 'Single inference step'
            inps = [epsilons[0]] + outputs_info[:-1] + non_seqs
            outs = self.step_infer(*inps)
            q, i_cost = self.unpack_infer(outs)
            qs = T.concatenate([q0[None, :, :], q[None, :, :]], axis=0)
            i_costs = [i_cost]

        elif self.n_inference_steps == 0:
            print 'No inference steps'
            qs = q0[None, :, :]
            i_costs = [T.constant(0.).astype(floatX)]

        if self.pass_gradients:
            constants = []
        else:
            constants = [qs]

        rval = OrderedDict(
            qk=qs[-1],
            qs=qs,
            i_costs=i_costs
        )

        return rval, constants, updates

    def __call__(self, x, y,
                 stride=10,
                 **model_args):

        model = self.model

        inference_outs, _, updates = self.inference(x, y)

        qs = inference_outs['qs']

        if self.n_inference_steps > stride and stride != 0:
            steps = [0, 1] + range(stride, self.n_inference_steps, stride)
            steps = steps[:-1] + [self.n_inference_steps - 1]
        elif self.n_inference_steps > 0:
            steps = [0, self.n_inference_steps - 1]
        else:
            steps = [0]

        full_results = OrderedDict()
        samples = OrderedDict()
        for i in steps:
            qk  = qs[i]
            results_k, samples_k = model(x, y, qk, **model_args)
            samples_k['q'] = qk
            update_dict_of_lists(full_results, **results_k)
            update_dict_of_lists(samples, **samples_k)

        results = OrderedDict()
        for k, v in full_results.iteritems():
            results[k] = v[-1]
            results[k + '0'] = v[0]
            results['d_' + k] = v[0] - v[-1]

        return results, samples, full_results, updates
"""Code adapted from https://github.com/mjacar/pytorch-trpo."""
from __future__ import absolute_import, division, print_function

from copy import deepcopy
from itertools import chain

import numpy as np
import torch
import torch.nn.functional as F
from torch import distributions
from torch.distributions import kl_divergence
from torch.distributions.categorical import Categorical
from torch.nn.utils.convert_parameters import (_check_param_device,
                                               parameters_to_vector,
                                               vector_to_parameters,)

import ray
from ray.rllib.a3c.shared_torch_policy import SharedTorchPolicy
from ray.rllib.models.pytorch.misc import convert_batch
# TODO(alok): use `process_rollout`
from ray.rllib.utils.process_rollout import discount, process_rollout


def explained_variance_1d(ypred, y):
    """
    Var[ypred - y] / var[y].
    https://www.quora.com/What-is-the-meaning-proportion-of-variance-explained-in-linear-regression
    """
    assert y.ndim == 1 and ypred.ndim == 1
    vary = np.var(y)
    return np.nan if vary == 0 else 1 - np.var(y - ypred) / vary


def vector_to_gradient(v, parameters):
    # TODO(alok) may have to rm the .data from v
    r"""Convert one vector representing the
    gradient to the .grad of the parameters.

    Arguments:
        v (Tensor): a single vector represents the parameters of a model.
        parameters (Iterable[Tensor]): an iterator of Tensors that are the
            parameters of a model.
    """
    # Ensure v of type Tensor
    if not isinstance(v, torch.Tensor):
        raise TypeError('expected torch.Tensor, but got: {}'.format(
            torch.typename(v)))
    # Flag for the device where the parameter is located
    param_device = None

    # Pointer for slicing the vector for each parameter
    pointer = 0
    for param in parameters:
        # Ensure the parameters are located in the same device
        param_device = _check_param_device(param, param_device)

        # The length of the parameter
        num_param = np.prod(param.grad.shape)
        # Slice the vector, reshape it, and replace the old data of the parameter
        param.grad.data = v[pointer:pointer + num_param].view(
            param.shape).detach()

        # Increment the pointer
        pointer += num_param


class TRPOPolicy(SharedTorchPolicy):
    def __init__(
            self,
            registry,
            ob_space,
            ac_space,
            config,
            **kwargs,
    ):
        super().__init__(
            registry,
            ob_space,
            ac_space,
            config,
            **kwargs,
        )

    def _evaluate_action_dists(self, obs, *args):
        logits, _ = self._model(obs)
        # TODO(alok): Handle continuous case since this assumes a
        # Categorical distribution.
        action_dists = F.softmax(logits, dim=1)
        return action_dists

    def mean_kl(self):
        """Returns an estimate of the average KL divergence between a given
        policy and self._model."""
        # TODO(alok): Handle continuous case since this assumes a
        # Categorical distribution.
        new_prob = F.softmax(
            self._model(self._states)[0], dim=1).detach() + 1e-8
        old_prob = F.softmax(self._model(self._states)[0], dim=1)

        new_prob, old_prob = Categorical(probs=new_prob), Categorical(
            probs=old_prob)

        return kl_divergence(new_prob, old_prob).mean()

    def HVP(self, v):
        """Returns the product of the Hessian of the KL divergence and the
        given vector."""

        self._model.zero_grad()
        print(f'KL: {self._kl}')

        g_kl = torch.autograd.grad(
            self._kl, self._model.parameters(), create_graph=True)
        gvp = torch.cat([grad.view(-1) for grad in g_kl]).dot(v)

            outputs=self._kl,
            inputs=chain(self._model.hidden_layers.parameters(),
                         self._model.logits.parameters()),
            create_graph=True,
        H = torch.autograd.grad(
            outputs=gvp,
            inputs=chain(self._model.hidden_layers.parameters(),
                         self._model.logits.parameters()),
            create_graph=True,
        )
        fisher_vector_product = torch.cat(
            [grad.contiguous().view(-1) for grad in H]).detach()

        return fisher_vector_product + (self.config['cg_damping'] * v.detach())

    def conjugate_gradient(self, b, cg_iters=10):
        """Returns F^(-1)b where F is the Hessian of the KL divergence."""
        p = b.clone().detach()
        r = b.clone().detach()
        x = np.zeros_like(b.numpy())

        rdotr = r.double().dot(r.double())

        for _ in range(cg_iters):
            z = self.HVP(p).squeeze(0)
            v = rdotr / p.double().dot(z.double())
            x += v * p.numpy()
            r -= v * z
            newrdotr = r.double().dot(r.double())
            mu = newrdotr / rdotr
            p = r + mu * p
            rdotr = newrdotr

            if rdotr < self.config['residual_tol']:
                break
        return x

    def surrogate_loss(self, params):
        """Returns the surrogate loss wrt the given parameter vector params."""

        new_policy = deepcopy(self._model)
        vector_to_parameters(params, new_policy.parameters())
        # TODO adjust only action params
        vector_to_parameters(
            params,
            chain(self._model.hidden_layers.parameters(),
                  self._model.logits.parameters()))

        EPSILON = 1e-8

        prob_new = new_policy(self._states)[0].gather(
            1, self._actions.view(-1, 1)).detach()
        prob_old = self._model(self._states)[0].gather(
            1, self._actions.view(-1, 1)).detach() + EPSILON

        return -torch.mean((prob_new / prob_old) * self._adv)

    def linesearch(self, x, fullstep, expected_improve_rate):
        """Returns the scaled gradient that would improve the loss.

        Found via linesearch.
        """

        accept_ratio = 0.1
        max_backtracks = 10

        fval = self.surrogate_loss(x)

        for stepfrac in .5**np.arange(max_backtracks):

            g = stepfrac * fullstep

            xnew = x.numpy() + g
            newfval = self.surrogate_loss(torch.from_numpy(xnew))
            actual_improve = fval - newfval
            expected_improve = expected_improve_rate * stepfrac

            if actual_improve / expected_improve > accept_ratio and actual_improve > 0:
                return g

        # If no improvement could be obtained, return 0 gradient
        else:
            return 0

    def _backward(self, batch):
        """Fills gradient buffers up."""

        states, actions, advs, rewards, _ = convert_batch(batch)
        values, _, entropy = self._evaluate(states, actions)
        action_dists = self._evaluate_action_dists(states)

        bs = self.config['batch_size']

        num_batches = len(actions) // bs if len(actions) % bs == 0 else (
            len(actions) // bs) + 1

        for batch_n in range(num_batches):
            _slice = slice(batch_n * bs, (batch_n + 1) * bs)

            self._states = states[_slice]
            self._actions = actions[_slice]
            self._action_dists = action_dists[_slice]
            self._adv = advs[_slice]

            self._kl = self.mean_kl()

            # Calculate the surrogate loss as the element-wise product of the
            # advantage and the probability ratio of actions taken.
            # TODO(alok) Do we need log probs or the actual probabilities here?
            new_prob = self._action_dists.gather(1, self._actions.view(-1, 1))
            old_prob = new_prob.detach() + 1e-8

            prob_ratio = new_prob / old_prob
            surrogate_loss = -torch.mean(prob_ratio * self._adv) - (
                self.config['ent_coeff'] * entropy)

            # Gradient wrt policy
            self._model.zero_grad()

            surrogate_loss.backward(retain_graph=True)

            g = parameters_to_vector(
                [v.grad for v in self._model.parameters()]).squeeze(0)

            if g.nonzero().shape[0]:
                step_dir = self.conjugate_gradient(-g)
                _step_dir = torch.from_numpy(step_dir)

                # Do line search to determine the stepsize of params in the direction of step_dir
                shs = step_dir.dot(self.HVP(self._kl, _step_dir).numpy().T) / 2
                lm = np.sqrt(shs / self.config['max_kl'])
                fullstep = step_dir / lm
                g_step_dir = -g.dot(_step_dir).detach()[0]
                grad = self.linesearch(
                    x=parameters_to_vector(self._model.parameters()),
                    fullstep=fullstep,
                    expected_improve_rate=g_step_dir / lm,
                )

                # Here we fill the gradient buffers
                if not any(np.isnan(grad.numpy())):
                    vector_to_gradient(grad, self._model.parameters())

            # Also get gradient wrt value function
            value_err = F.mse_loss(values, rewards)
            value_err.backward()
        g = parameters_to_vector([
            p.grad for p in chain(self._model.hidden_layers.parameters(),
                                  self._model.logits.parameters())
        ]).squeeze(0)
            grad = self.linesearch(
                x=parameters_to_vector(
                    chain(self._model.hidden_layers.parameters(),
                          self._model.logits.parameters())),
                fullstep=fullstep,
                expected_improve_rate=g_step_dir / lm,
            )
            # Here we fill the gradient buffers
            if not any(np.isnan(grad)):
                vector_to_gradient(
                    torch.from_numpy(grad),
                    chain(self._model.hidden_layers.parameters(),
                          self._model.logits.parameters()))
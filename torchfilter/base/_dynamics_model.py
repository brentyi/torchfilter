"""Private module; avoid importing from directly.
"""

import abc
from typing import List, Tuple

import fannypack
import torch
import torch.nn as nn
from overrides import overrides

from .. import types


class DynamicsModel(nn.Module, abc.ABC):
    """Base class for a generic differentiable dynamics model, with additive white
    Gaussian noise.

    Subclasses should override either `forward` or `forward_loop` for computing dynamics
    estimates.
    """

    def __init__(self, *, state_dim: int) -> None:
        super().__init__()

        self.state_dim = state_dim
        """int: Dimensionality of our state."""

    # @overrides
    def forward(
        self,
        *,
        initial_states: types.StatesTorch,
        controls: types.ControlsTorch,
    ) -> Tuple[types.StatesTorch, types.ScaleTrilTorch]:
        """Dynamics model forward pass, single timestep.

        Computes both predicted states and uncertainties. Note that uncertainties
        correspond to the (Cholesky decompositions of the) "Q" matrices in a standard
        linear dynamical system w/ additive white Gaussian noise. In other words, they
        should be lower triangular and not accumulate -- the uncertainty at at time `t`
        should be computed as if the estimate at time `t - 1` is a ground-truth input.

        By default, this is implemented by bootstrapping the `forward_loop()`
        method.

        Args:
            initial_states (torch.Tensor): Initial states of our system. Shape should be
                `(N, state_dim)`.
            controls (dict or torch.Tensor): Control inputs. Should be either a
                dict of tensors or tensor of size `(N, ...)`.

        Returns:
            Tuple[torch.Tensor, torch.Tensor]: Predicted states & uncertainties. States
            should have shape `(N, state_dim)`, and uncertainties should be lower
            triangular with shape `(N, state_dim, state_dim).`
        """

        # Wrap our control inputs
        #
        # If the input is a dictionary of tensors, this provides a
        # tensor-like interface for slicing, accessing shape, etc
        controls_wrapped = fannypack.utils.SliceWrapper(controls)

        # Call `forward_loop()` with a single timestep
        predictions, scale_trils = self.forward_loop(
            initial_states=initial_states, controls=controls_wrapped[None, ...]
        )
        assert predictions.shape[0] == 1
        assert scale_trils.shape[0] == 1
        return predictions[0], scale_trils[0]

    def forward_loop(
        self, *, initial_states: types.StatesTorch, controls: types.ControlsTorch
    ) -> Tuple[types.StatesTorch, torch.Tensor]:
        """Dynamics model forward pass, over sequence length `T` and batch size
        `N`.  By default, this is implemented by iteratively calling
        `forward()`.

        Computes both predicted states and uncertainties. Note that uncertainties
        correspond to the (Cholesky decompositions of the) "Q" matrices in a standard
        linear dynamical system w/ additive white Gaussian noise. In other words, they
        should be lower triangular and not accumulate -- the uncertainty at at time `t`
        should be computed as if the estimate at time `t - 1` is a ground-truth input.

        To inject code between timesteps (for example, to inspect hidden state),
        use `register_forward_hook()`.

        Args:
            initial_states (torch.Tensor): Initial states to pass to our
                dynamics model. Shape should be `(N, state_dim)`.
            controls (dict or torch.Tensor): Control inputs. Should be either a
                dict of tensors or tensor of size `(T, N, ...)`.
        Returns:
            Tuple[torch.Tensor, torch.Tensor]: Predicted states & uncertainties. States
            should have shape `(T, N, state_dim)`, and uncertainties should be lower
            triangular with shape `(T, N, state_dim, state_dim).`
        """

        # Wrap our control inputs
        #
        # If the input is a dictionary of tensors, this provides a
        # tensor-like interface for slicing, accessing shape, etc
        controls_wrapped = fannypack.utils.SliceWrapper(controls)

        # Get sequence length (T), batch size (N)
        T, N = controls_wrapped.shape[:2]
        assert initial_states.shape == (N, self.state_dim)
        assert T > 0

        # Dynamics forward pass
        predictions_list: List[types.StatesTorch] = []
        scale_trils_list: List[torch.Tensor] = []

        constant_noise = True
        prediction = initial_states

        for t in range(T):
            # Compute state estimate for a single timestep
            # We use __call__ to make sure hooks are dispatched correctly
            prediction, scale_tril = self(
                initial_states=prediction, controls=controls_wrapped[t]
            )

            # Check if noise is time-varying
            if t >= 1 and (
                scale_tril.data_ptr() != scale_trils_list[-1].data_ptr()  # type: ignore
                or scale_tril.stride() != scale_trils_list[-1].stride()
            ):
                constant_noise = False

            # Validate & add to output
            assert prediction.shape == (N, self.state_dim)
            assert scale_tril.shape == (N, self.state_dim, self.state_dim)
            predictions_list.append(prediction)
            scale_trils_list.append(scale_tril)

        # Stack predictions
        predictions = torch.stack(predictions_list, dim=0)

        # Stack uncertainties
        if constant_noise:
            # If our noise is constant, we save memory by returning a strided view of
            # the first tensor in the list
            scale_trils = scale_trils_list[0][None, :, :, :].expand(
                T, N, self.state_dim, self.state_dim
            )
        else:
            # If our noise is time-varying, stack normally
            scale_trils = torch.stack(scale_trils_list, dim=0)

        # Validate & return state estimates
        assert predictions.shape == (T, N, self.state_dim)
        assert scale_trils.shape == (T, N, self.state_dim, self.state_dim)
        return predictions, scale_trils

    def jacobian(
        self,
        initial_states: types.StatesTorch,
        controls: types.ControlsTorch,
    ) -> torch.Tensor:
        """Returns Jacobian of the dynamics model.

        Args:
            states (torch.Tensor): Current state, size `(N, state_dim)`.
            controls (dict or torch.Tensor): Control inputs. Should be either a
                dict of tensors or tensor of size `(N, ...)`.

        Returns:
            torch.Tensor: Jacobian, size `(N, state_dim, state_dim)`
        """
        with torch.enable_grad():
            x = initial_states.detach().clone()

            N, ndim = x.shape
            assert ndim == self.state_dim
            x = x[:, None, :].expand((N, ndim, ndim))
            controls = fannypack.utils.SliceWrapper(controls).map(
                lambda tensor: torch.repeat_interleave(tensor, repeats=ndim, dim=0)
            )
            x.requires_grad_(True)
            y = self(initial_states=x.reshape((-1, ndim)), controls=controls)[
                0
            ].reshape((N, ndim, ndim))
            mask = torch.eye(ndim, device=x.device).repeat(N, 1, 1)
            jac = torch.autograd.grad(y, x, mask, create_graph=True)

        return jac[0]

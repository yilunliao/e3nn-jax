import itertools
from functools import lru_cache, partial
from math import sqrt
from typing import Any, List, NamedTuple, Optional

import jax
import jax.numpy as jnp

from e3nn_jax import Irreps, IrrepsData, wigner_3j
from e3nn_jax.util import prod

from ._einsum import einsum as opt_einsum


def _sum_tensors(xs, shape, empty_return_none=False):
    xs = [x for x in xs if x is not None]
    if len(xs) > 0:
        out = xs[0].reshape(shape)
        for x in xs[1:]:
            out = out + x.reshape(shape)
        return out
    if empty_return_none:
        return None
    return jnp.zeros(shape)


def _flat_concatenate(xs):
    if any(x is None for x in xs):
        return None
    if len(xs) > 0:
        return jnp.concatenate([x.flatten() for x in xs])
    return jnp.zeros((0,))


class Instruction(NamedTuple):
    i_in1: int
    i_in2: int
    i_out: int
    connection_mode: str
    has_weight: bool
    path_weight: float
    path_shape: tuple


class FunctionalTensorProduct:
    r"""Tensor product of two tensors.

    Args:
        irreps_in1: :class:`~e3nn_jax.Irreps` of the first tensor.
        irreps_in2: :class:`~e3nn_jax.Irreps` of the second tensor.
        irreps_out: :class:`~e3nn_jax.Irreps` of the output tensor.
        instructions: List of instructions.
            ``[(i_in1, i_in2, i_out, connection_mode, has_weight, (path_weight)), ...]``
            - i_in1, i_in2, i_out are indices of the irreps_in1, irreps_in2, irreps_out.
            - connection_mode is one of ``uvw``, ``uvu``, ``uvv``, ``uuw``, ``uuu``, ``uvuv``
            - has_weight is a boolean indicating whether the instruction has a weight.
            - path_weight (optional) is the weight of the path.

        in1_var: Variance of the first tensor.
        in2_var: Variance of the second tensor.
        out_var: Variance of the output tensor.
        irrep_normalization: Normalization of the tensors. `component` or `norm`.
        path_normalization: Normalization of the paths. `element` or `path`.
    """
    irreps_in1: Irreps
    irreps_in2: Irreps
    irreps_out: Irreps
    instructions: List[Instruction]
    output_mask: jnp.ndarray

    def __init__(
        self,
        irreps_in1: Any,
        irreps_in2: Any,
        irreps_out: Any,
        instructions: List[Any],
        in1_var: Optional[List[float]] = None,
        in2_var: Optional[List[float]] = None,
        out_var: Optional[List[float]] = None,
        irrep_normalization: str = 'component',
        path_normalization: str = 'element',
    ):
        assert irrep_normalization in ['component', 'norm', 'none']
        assert path_normalization in ['element', 'path']

        self.irreps_in1 = Irreps(irreps_in1)
        self.irreps_in2 = Irreps(irreps_in2)
        self.irreps_out = Irreps(irreps_out)
        del irreps_in1, irreps_in2, irreps_out

        instructions = [x if len(x) == 6 else x + (1.0,) for x in instructions]
        instructions = [
            Instruction(
                i_in1, i_in2, i_out, connection_mode, has_weight, path_weight,
                {
                    'uvw': (self.irreps_in1[i_in1].mul, self.irreps_in2[i_in2].mul, self.irreps_out[i_out].mul),
                    'uvu': (self.irreps_in1[i_in1].mul, self.irreps_in2[i_in2].mul),
                    'uvv': (self.irreps_in1[i_in1].mul, self.irreps_in2[i_in2].mul),
                    'uuw': (self.irreps_in1[i_in1].mul, self.irreps_out[i_out].mul),
                    'uuu': (self.irreps_in1[i_in1].mul,),
                    'uvuv': (self.irreps_in1[i_in1].mul, self.irreps_in2[i_in2].mul),
                    'uvu<v': (self.irreps_in1[i_in1].mul * (self.irreps_in2[i_in2].mul - 1) // 2,),
                    'u<vw': (self.irreps_in1[i_in1].mul * (self.irreps_in2[i_in2].mul - 1) // 2, self.irreps_out[i_out].mul),
                }[connection_mode],
            )
            for i_in1, i_in2, i_out, connection_mode, has_weight, path_weight in instructions
        ]

        if in1_var is None:
            in1_var = [1.0 for _ in range(len(self.irreps_in1))]

        if in2_var is None:
            in2_var = [1.0 for _ in range(len(self.irreps_in2))]

        if out_var is None:
            out_var = [1.0 for _ in range(len(self.irreps_out))]

        def num_elements(ins):
            return {
                'uvw': (self.irreps_in1[ins.i_in1].mul * self.irreps_in2[ins.i_in2].mul),
                'uvu': self.irreps_in2[ins.i_in2].mul,
                'uvv': self.irreps_in1[ins.i_in1].mul,
                'uuw': self.irreps_in1[ins.i_in1].mul,
                'uuu': 1,
                'uvuv': 1,
                'uvu<v': 1,
                'u<vw': self.irreps_in1[ins.i_in1].mul * (self.irreps_in2[ins.i_in2].mul - 1) // 2,
            }[ins.connection_mode]

        normalization_coefficients = []
        for ins in instructions:
            mul_ir_in1 = self.irreps_in1[ins.i_in1]
            mul_ir_in2 = self.irreps_in2[ins.i_in2]
            mul_ir_out = self.irreps_out[ins.i_out]
            assert mul_ir_in1.ir.p * mul_ir_in2.ir.p == mul_ir_out.ir.p
            assert abs(mul_ir_in1.ir.l - mul_ir_in2.ir.l) <= mul_ir_out.ir.l <= mul_ir_in1.ir.l + mul_ir_in2.ir.l
            assert ins.connection_mode in ['uvw', 'uvu', 'uvv', 'uuw', 'uuu', 'uvuv', 'uvu<v', 'u<vw']

            alpha = None

            if irrep_normalization == 'component':
                alpha = mul_ir_out.ir.dim
            if irrep_normalization == 'norm':
                alpha = mul_ir_in1.ir.dim * mul_ir_in2.ir.dim
            if irrep_normalization == 'none':
                alpha = 1

            x = None

            if path_normalization == 'element':
                x = sum(
                    in1_var[i.i_in1] * in2_var[i.i_in2] * num_elements(i)
                    for i in instructions
                    if i.i_out == ins.i_out
                )
            if path_normalization == 'path':
                x = in1_var[ins.i_in1] * in2_var[ins.i_in2] * num_elements(ins)
                x *= len([i for i in instructions if i.i_out == ins.i_out])

            if x > 0.0:
                alpha /= x
            alpha *= out_var[ins.i_out]
            alpha *= ins.path_weight

            normalization_coefficients += [sqrt(alpha)]

        self.instructions = [
            Instruction(ins.i_in1, ins.i_in2, ins.i_out, ins.connection_mode, ins.has_weight, alpha, ins.path_shape)
            for ins, alpha in zip(instructions, normalization_coefficients)
        ]

        with jax.ensure_compile_time_eval():
            if self.irreps_out.dim > 0:
                self.output_mask = jnp.concatenate([
                    jnp.ones(mul_ir.dim)
                    if any(
                        (ins.i_out == i_out) and (ins.path_weight != 0) and (0 not in ins.path_shape)
                        for ins in self.instructions
                    )
                    else jnp.zeros(mul_ir.dim)
                    for i_out, mul_ir in enumerate(self.irreps_out)
                ])
            else:
                self.output_mask = jnp.ones(0)

    @partial(jax.jit, static_argnums=(0,), static_argnames=('specialized_code', 'optimize_einsums', 'custom_einsum_vjp', 'fuse_all'))
    @partial(jax.profiler.annotate_function, name="TensorProduct.left_right")
    def left_right(self, weights, input1, input2=None, *, specialized_code=False, optimize_einsums=True, custom_einsum_vjp=False, fuse_all=False) -> IrrepsData:
        r"""Compute the tensor product of two input tensors.

        Args:
            weights (array or list of arrays): The weights of the tensor product.
            input1 (array or list of arrays): The first input tensor.
            input2 (array or list of arrays): The second input tensor.
            specialized_code (bool): If True, use the specialized code for the
                tensor product.
            optimize_einsums (bool): If True, optimize the einsum code.
            custom_einsum_vjp (bool): If True, use the custom vjp for the einsum
                code.
            fuse_all (bool): If True, fuse all the einsums.

        Returns:
            `IrrepsData`: The output tensor.
        """
        if input2 is None:
            weights, input1, input2 = [], weights, input1

        # = Short-circut for zero dimensional =
        if self.irreps_in1.dim == 0 or self.irreps_in2.dim == 0 or self.irreps_out.dim == 0:
            return IrrepsData.zeros(self.irreps_out, ())

        if custom_einsum_vjp:
            assert optimize_einsums
            einsum = opt_einsum
        else:
            einsum = partial(jnp.einsum, optimize='optimal' if optimize_einsums else 'greedy')

        input1 = IrrepsData.new(self.irreps_in1, input1)
        input2 = IrrepsData.new(self.irreps_in2, input2)

        if isinstance(weights, list):
            assert len(weights) == len([ins for ins in self.instructions if ins.has_weight]), (len(weights), len([ins for ins in self.instructions if ins.has_weight]))
            weights_flat = _flat_concatenate(weights)
            weights_list = weights
        else:
            weights_flat = weights
            weights_list = []
            i = 0
            for ins in self.instructions:
                if ins.has_weight:
                    n = prod(ins.path_shape)
                    weights_list.append(weights[i:i+n].reshape(ins.path_shape))
                    i += n
            assert i == weights.size
        del weights

        assert all(x is None or x.ndim == 2 for x in input1.list), "the input of TensorProduct must be a list of 2D arrays"
        assert all(x is None or x.ndim == 2 for x in input2.list), "the input of TensorProduct must be a list of 2D arrays"

        if fuse_all:
            with jax.ensure_compile_time_eval():
                num_path = weights_flat.size
                has_path_with_no_weights = any(not ins.has_weight for ins in self.instructions)
                i = 0

                if has_path_with_no_weights:
                    num_path += 1
                    i += 1

                big_w3j = jnp.zeros((num_path, self.irreps_in1.dim, self.irreps_in2.dim, self.irreps_out.dim))
                for ins in self.instructions:
                    mul_ir_in1 = self.irreps_in1[ins.i_in1]
                    mul_ir_in2 = self.irreps_in2[ins.i_in2]
                    mul_ir_out = self.irreps_out[ins.i_out]
                    m1, m2, mo = mul_ir_in1.mul, mul_ir_in2.mul, mul_ir_out.mul
                    d1, d2, do = mul_ir_in1.ir.dim, mul_ir_in2.ir.dim, mul_ir_out.ir.dim
                    s1 = self.irreps_in1[:ins.i_in1].dim
                    s2 = self.irreps_in2[:ins.i_in2].dim
                    so = self.irreps_out[:ins.i_out].dim

                    w3j = wigner_3j(mul_ir_in1.ir.l, mul_ir_in2.ir.l, mul_ir_out.ir.l)

                    def set_w3j(i, u, v, w):
                        return big_w3j.at[i, s1+u*d1: s1+(u+1)*d1, s2+v*d2: s2+(v+1)*d2, so+w*do: so+(w+1)*do].add(ins.path_weight * w3j)

                    if ins.connection_mode == 'uvw':
                        assert ins.has_weight
                        for u, v, w in itertools.product(range(m1), range(m2), range(mo)):
                            big_w3j = set_w3j(i, u, v, w)
                            i += 1
                    elif ins.connection_mode == 'uvu':
                        assert ins.has_weight
                        for u, v in itertools.product(range(m1), range(m2)):
                            big_w3j = set_w3j(i, u, v, u)
                            i += 1
                    elif ins.connection_mode == 'uvv':
                        assert ins.has_weight
                        for u, v in itertools.product(range(m1), range(m2)):
                            big_w3j = set_w3j(i, u, v, v)
                            i += 1
                    elif ins.connection_mode == 'uuu':
                        for u in range(m1):
                            if ins.has_weight:
                                big_w3j = set_w3j(i, u, u, u)
                                i += 1
                            else:
                                big_w3j = set_w3j(0, u, u, u)
                    else:
                        assert False

            if has_path_with_no_weights and big_w3j.shape[0] == 1:
                big_w3j = big_w3j.reshape(big_w3j.shape[1:])
                out = einsum("ijk,i,j->k", big_w3j, input1.contiguous, input2.contiguous)
            else:
                if has_path_with_no_weights:
                    weights_flat = jnp.concatenate([jnp.ones((1,)), weights_flat])

                out = einsum("p,pijk,i,j->k", weights_flat, big_w3j, input1.contiguous, input2.contiguous)
            return IrrepsData.from_contiguous(self.irreps_out, out)

        @lru_cache(maxsize=None)
        def multiply(in1, in2, mode):
            if mode == 'uv':
                return einsum('ui,vj->uvij', input1.list[in1], input2.list[in2])
            if mode == 'uu':
                return einsum('ui,uj->uij', input1.list[in1], input2.list[in2])

        weight_index = 0

        out_list = []

        for ins in self.instructions:
            mul_ir_in1 = self.irreps_in1[ins.i_in1]
            mul_ir_in2 = self.irreps_in2[ins.i_in2]
            mul_ir_out = self.irreps_out[ins.i_out]

            if ins.has_weight:
                w = weights_list[weight_index]
                assert w.shape == ins.path_shape
                weight_index += 1

            if mul_ir_in1.dim == 0 or mul_ir_in2.dim == 0 or mul_ir_out.dim == 0:
                continue

            x1 = input1.list[ins.i_in1]
            x2 = input2.list[ins.i_in2]
            if x1 is None or x2 is None:
                out_list += [None]
                continue

            xx = multiply(ins.i_in1, ins.i_in2, ins.connection_mode[:2])

            with jax.ensure_compile_time_eval():
                w3j = wigner_3j(mul_ir_in1.ir.l, mul_ir_in2.ir.l, mul_ir_out.ir.l)
                w3j = ins.path_weight * w3j

            if ins.connection_mode == 'uvw':
                assert ins.has_weight
                if specialized_code and (mul_ir_in1.ir.l, mul_ir_in2.ir.l, mul_ir_out.ir.l) == (0, 0, 0):
                    out = ins.path_weight * einsum("uvw,uv->w", w, xx.reshape(mul_ir_in1.dim, mul_ir_in2.dim))
                elif specialized_code and mul_ir_in1.ir.l == 0:
                    out = ins.path_weight * einsum("uvw,u,vj->wj", w, x1.reshape(mul_ir_in1.dim), x2) / sqrt(mul_ir_out.ir.dim)
                elif specialized_code and mul_ir_in2.ir.l == 0:
                    out = ins.path_weight * einsum("uvw,ui,v->wi", w, x1, x2.reshape(mul_ir_in2.dim)) / sqrt(mul_ir_out.ir.dim)
                elif specialized_code and mul_ir_out.ir.l == 0:
                    out = ins.path_weight * einsum("uvw,ui,vi->w", w, x1, x2) / sqrt(mul_ir_in1.ir.dim)
                else:
                    out = einsum("uvw,ijk,uvij->wk", w, w3j, xx)
            if ins.connection_mode == 'uvu':
                assert mul_ir_in1.mul == mul_ir_out.mul
                if ins.has_weight:
                    if specialized_code and (mul_ir_in1.ir.l, mul_ir_in2.ir.l, mul_ir_out.ir.l) == (0, 0, 0):
                        out = ins.path_weight * einsum("uv,u,v->u", w, x1.reshape(mul_ir_in1.dim), x2.reshape(mul_ir_in2.dim))
                    elif specialized_code and mul_ir_in1.ir.l == 0:
                        out = ins.path_weight * einsum("uv,u,vj->uj", w, x1.reshape(mul_ir_in1.dim), x2) / sqrt(mul_ir_out.ir.dim)
                    elif specialized_code and mul_ir_in2.ir.l == 0:
                        out = ins.path_weight * einsum("uv,ui,v->ui", w, x1, x2.reshape(mul_ir_in2.dim)) / sqrt(mul_ir_out.ir.dim)
                    elif specialized_code and mul_ir_out.ir.l == 0:
                        out = ins.path_weight * einsum("uv,ui,vi->u", w, x1, x2) / sqrt(mul_ir_in1.ir.dim)
                    else:
                        out = einsum("uv,ijk,uvij->uk", w, w3j, xx)
                else:
                    # not so useful operation because v is summed
                    out = einsum("ijk,uvij->uk", w3j, xx)
            if ins.connection_mode == 'uvv':
                assert mul_ir_in2.mul == mul_ir_out.mul
                if ins.has_weight:
                    if specialized_code and (mul_ir_in1.ir.l, mul_ir_in2.ir.l, mul_ir_out.ir.l) == (0, 0, 0):
                        out = ins.path_weight * einsum("uv,u,v->v", w, x1.reshape(mul_ir_in1.dim), x2.reshape(mul_ir_in2.dim))
                    elif specialized_code and mul_ir_in1.ir.l == 0:
                        out = ins.path_weight * einsum("uv,u,vj->vj", w, x1.reshape(mul_ir_in1.dim), x2) / sqrt(mul_ir_out.ir.dim)
                    elif specialized_code and mul_ir_in2.ir.l == 0:
                        out = ins.path_weight * einsum("uv,ui,v->vi", w, x1, x2.reshape(mul_ir_in2.dim)) / sqrt(mul_ir_out.ir.dim)
                    elif specialized_code and mul_ir_out.ir.l == 0:
                        out = ins.path_weight * einsum("uv,ui,vi->v", w, x1, x2) / sqrt(mul_ir_in1.ir.dim)
                    else:
                        out = einsum("uv,ijk,uvij->vk", w, w3j, xx)
                else:
                    # not so useful operation because u is summed
                    out = einsum("ijk,uvij->vk", w3j, xx)
            if ins.connection_mode == 'uuw':
                assert mul_ir_in1.mul == mul_ir_in2.mul
                if ins.has_weight:
                    out = einsum("uw,ijk,uij->wk", w, w3j, xx)
                else:
                    # equivalent to tp(x, y, 'uuu').sum('u')
                    assert mul_ir_out.mul == 1
                    out = einsum("ijk,uij->k", w3j, xx)
            if ins.connection_mode == 'uuu':
                assert mul_ir_in1.mul == mul_ir_in2.mul == mul_ir_out.mul
                if ins.has_weight:
                    out = einsum("u,ijk,uij->uk", w, w3j, xx)
                else:
                    out = einsum("ijk,uij->uk", w3j, xx)
            if ins.connection_mode == 'uvuv':
                assert mul_ir_in1.mul * mul_ir_in2.mul == mul_ir_out.mul
                if ins.has_weight:
                    out = einsum("uv,ijk,uvij->uvk", w, w3j, xx)
                else:
                    out = einsum("ijk,uvij->uvk", w3j, xx)
            if ins.connection_mode == 'uvu<v':
                assert mul_ir_in1.mul == mul_ir_in2.mul
                assert mul_ir_in1.mul * (mul_ir_in1.mul - 1) // 2 == mul_ir_out.mul
                i = jnp.triu_indices(mul_ir_in1.mul, 1)
                xx = xx[i[0], i[1]]  # uvij -> wij
                if ins.has_weight:
                    out = einsum("w,ijk,wij->wk", w, w3j, xx)
                else:
                    out = einsum("ijk,wij->wk", w3j, xx)
            if ins.connection_mode == 'u<vw':
                assert mul_ir_in1.mul == mul_ir_in2.mul
                assert ins.has_weight
                i = jnp.triu_indices(mul_ir_in1.mul, 1)
                xx = multiply(ins.i_in1, ins.i_in2, 'uv')
                xx = xx[i[0], i[1]]  # uvij -> qij
                out = einsum("qw,ijk,qij->wk", w, w3j, xx)

            out_list += [out]

        out = [
            _sum_tensors(
                [out for ins, out in zip(self.instructions, out_list) if ins.i_out == i_out],
                shape=(mul_ir_out.mul, mul_ir_out.ir.dim),
                empty_return_none=True,
            )
            for i_out, mul_ir_out in enumerate(self.irreps_out)
        ]
        return IrrepsData.from_list(self.irreps_out, out, ())

    @partial(jax.jit, static_argnums=(0,), static_argnames=('optimize_einsums', 'custom_einsum_vjp'))
    @partial(jax.profiler.annotate_function, name="TensorProduct.right")
    def right(self, weights, input2=None, *, optimize_einsums=False, custom_einsum_vjp=False):
        if input2 is None:
            weights, input2 = [], weights

        # = Short-circut for zero dimensional =
        if self.irreps_in1.dim == 0 or self.irreps_in2.dim == 0 or self.irreps_out.dim == 0:
            return jnp.zeros((self.irreps_in1.dim, self.irreps_out.dim,))

        # = extract individual input irreps =
        input2 = IrrepsData.new(self.irreps_in2, input2)

        if custom_einsum_vjp:
            assert optimize_einsums
            einsum = opt_einsum
        else:
            einsum = partial(jnp.einsum, optimize='optimal' if optimize_einsums else 'greedy')

        weight_index = 0

        out_list = []

        for ins in self.instructions:
            mul_ir_in1 = self.irreps_in1[ins.i_in1]
            mul_ir_in2 = self.irreps_in2[ins.i_in2]
            mul_ir_out = self.irreps_out[ins.i_out]

            x2 = input2.list[ins.i_in2]

            if ins.has_weight:
                w = weights[weight_index]
                assert w.shape == ins.path_shape, (w.shape, ins.path_shape, weight_index, ins)
                weight_index += 1

            if mul_ir_in1.dim == 0 or mul_ir_in2.dim == 0 or mul_ir_out.dim == 0:
                # TODO add tests for this case
                out_list += [jnp.zeros((mul_ir_in1.dim, mul_ir_out.dim))]
                continue

            with jax.ensure_compile_time_eval():
                w3j = wigner_3j(mul_ir_in1.ir.l, mul_ir_in2.ir.l, mul_ir_out.ir.l)

            if ins.connection_mode == 'uvw':
                assert ins.has_weight
                out = einsum("uvw,ijk,vj->uiwk", w, w3j, x2)
            if ins.connection_mode == 'uvu':
                assert mul_ir_in1.mul == mul_ir_out.mul
                if ins.has_weight:
                    out = einsum("uv,ijk,vj,uw->uiwk", w, w3j, x2, jnp.eye(mul_ir_in1.mul))
                else:
                    # not so useful operation because v is summed
                    out = einsum("ijk,vj,uw->uiwk", w3j, x2, jnp.eye(mul_ir_in1.mul))
            if ins.connection_mode == 'uvv':
                assert mul_ir_in2.mul == mul_ir_out.mul
                if ins.has_weight:
                    out = einsum("uv,ijk,vj->uivk", w, w3j, x2)
                else:
                    # not so useful operation because u is summed
                    out = einsum("ijk,vj,u->uivk", w3j, x2, jnp.ones((mul_ir_in1.mul,)))
            if ins.connection_mode == 'uuw':
                assert mul_ir_in1.mul == mul_ir_in2.mul
                if ins.has_weight:
                    out = einsum("uw,ijk,uj->uiwk", w, w3j, x2)
                else:
                    # equivalent to tp(x, y, 'uuu').sum('u')
                    assert mul_ir_out.mul == 1
                    out = einsum("ijk,uj->uik", w3j, x2)
            if ins.connection_mode == 'uuu':
                assert mul_ir_in1.mul == mul_ir_in2.mul == mul_ir_out.mul
                if ins.has_weight:
                    out = einsum("u,ijk,uj,uw->uiwk", w, w3j, x2, jnp.eye(mul_ir_in1.mul))
                else:
                    out = einsum("ijk,uj,uw->uiwk", w3j, x2, jnp.eye(mul_ir_in1.mul))
            if ins.connection_mode == 'uvuv':
                assert mul_ir_in1.mul * mul_ir_in2.mul == mul_ir_out.mul
                if ins.has_weight:
                    out = einsum("uv,ijk,vj,uw->uiwvk", w, w3j, x2, jnp.eye(mul_ir_in1.mul))
                else:
                    out = einsum("ijk,vj,uw->uiwvk", w3j, x2, jnp.eye(mul_ir_in1.mul))

            out = ins.path_weight * out

            out_list += [out.reshape(mul_ir_in1.dim, mul_ir_out.dim)]

        return jnp.concatenate([
            jnp.concatenate([
                _sum_tensors(
                    [out for ins, out in zip(self.instructions, out_list) if (ins.i_in1, ins.i_out) == (i_in1, i_out)],
                    shape=(mul_ir_in1.dim, mul_ir_out.dim),
                )
                for i_out, mul_ir_out in enumerate(self.irreps_out)
                if mul_ir_out.mul > 0
            ], axis=1)
            for i_in1, mul_ir_in1 in enumerate(self.irreps_in1)
            if mul_ir_in1.mul > 0
        ], axis=0)

    def __repr__(self):
        npath = sum(prod(i.path_shape) for i in self.instructions)
        nweight = sum(prod(i.path_shape) for i in self.instructions if i.has_weight)
        return (
            f"{self.__class__.__name__}"
            f"({self.irreps_in1.simplify()} x {self.irreps_in2.simplify()} "
            f"-> {self.irreps_out.simplify()} | {npath} paths | {nweight} weights)"
        )

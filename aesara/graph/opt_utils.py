import copy
from typing import Generator, Sequence, Union

import aesara
from aesara.graph.basic import (
    Apply,
    Variable,
    equal_computations,
    graph_inputs,
    vars_between,
)
from aesara.graph.fg import FunctionGraph
from aesara.graph.optdb import OptimizationQuery


def optimize_graph(
    fgraph: Union[Variable, FunctionGraph],
    include: Sequence[str] = ["canonicalize"],
    custom_opt=None,
    clone: bool = False,
    **kwargs
) -> Union[Variable, FunctionGraph]:
    """Easily optimize a graph.

    Parameters
    ==========
    fgraph:
        A ``FunctionGraph`` or ``Variable`` to be optimized.
    include:
        String names of the optimizations to be applied.  The default
        optimization is ``"canonicalization"``.
    custom_opt:
        A custom ``Optimization`` to also be applied.
    clone:
        Whether or not to clone the input graph before optimizing.
    **kwargs:
        Keyword arguments passed to the ``aesara.graph.optdb.OptimizationQuery`` object.
    """
    from aesara.compile import optdb

    return_only_out = False
    if not isinstance(fgraph, FunctionGraph):
        fgraph = FunctionGraph(outputs=[fgraph], clone=clone)
        return_only_out = True

    canonicalize_opt = optdb.query(OptimizationQuery(include=include, **kwargs))
    _ = canonicalize_opt.optimize(fgraph)

    if custom_opt:
        custom_opt.optimize(fgraph)

    if return_only_out:
        return fgraph.outputs[0]
    else:
        return fgraph


def is_same_graph_with_merge(var1, var2, givens=None):
    """
    Merge-based implementation of `aesara.graph.basic.is_same_graph`.

    See help on `aesara.graph.basic.is_same_graph` for additional documentation.

    """
    from aesara.graph.opt import MergeOptimizer

    if givens is None:
        givens = {}
    # Copy variables since the MergeOptimizer will modify them.
    copied = copy.deepcopy([var1, var2, givens])
    vars = copied[0:2]
    givens = copied[2]
    # Create FunctionGraph.
    inputs = list(graph_inputs(vars))
    # The clone isn't needed as we did a deepcopy and we cloning will
    # break the mapping in givens.
    fgraph = aesara.graph.fg.FunctionGraph(inputs, vars, clone=False)
    # Perform Variable substitution.
    for to_replace, replace_by in givens.items():
        fgraph.replace(to_replace, replace_by)
    # Perform merge optimization.
    MergeOptimizer().optimize(fgraph)
    # When two variables perform the same computations, they will have the same
    # owner in the optimized graph.
    # We need to be careful with the special case where the owner is None,
    # which happens when the graph is made of a single Variable.
    # We also need to make sure we replace a Variable if it is present in
    # `givens`.
    vars_replaced = [givens.get(v, v) for v in fgraph.outputs]
    o1, o2 = [v.owner for v in vars_replaced]
    if o1 is None and o2 is None:
        # Comparing two single-Variable graphs: they are equal if they are
        # the same Variable.
        return vars_replaced[0] == vars_replaced[1]
    else:
        return o1 is o2


def is_same_graph(var1, var2, givens=None):
    """
    Return True iff Variables `var1` and `var2` perform the same computation.

    By 'performing the same computation', we mean that they must share the same
    graph, so that for instance this function will return False when comparing
    (x * (y * z)) with ((x * y) * z).

    The current implementation is not efficient since, when possible, it
    verifies equality by calling two different functions that are expected to
    return the same output. The goal is to verify this assumption, to
    eventually get rid of one of them in the future.

    Parameters
    ----------
    var1
        The first Variable to compare.
    var2
        The second Variable to compare.
    givens
        Similar to the `givens` argument of `aesara.function`, it can be used
        to perform substitutions in the computational graph of `var1` and
        `var2`. This argument is associated to neither `var1` nor `var2`:
        substitutions may affect both graphs if the substituted variable
        is present in both.

    Examples
    --------

        ======  ======  ======  ======
        var1    var2    givens  output
        ======  ======  ======  ======
        x + 1   x + 1   {}      True
        x + 1   y + 1   {}      False
        x + 1   y + 1   {x: y}  True
        ======  ======  ======  ======

    """
    use_equal_computations = True

    if givens is None:
        givens = {}

    if not isinstance(givens, dict):
        givens = dict(givens)

    # Get result from the merge-based function.
    rval1 = is_same_graph_with_merge(var1=var1, var2=var2, givens=givens)

    if givens:
        # We need to build the `in_xs` and `in_ys` lists. To do this, we need
        # to be able to tell whether a variable belongs to the computational
        # graph of `var1` or `var2`.
        # The typical case we want to handle is when `to_replace` belongs to
        # one of these graphs, and `replace_by` belongs to the other one. In
        # other situations, the current implementation of `equal_computations`
        # is probably not appropriate, so we do not call it.
        ok = True
        in_xs = []
        in_ys = []
        # Compute the sets of all variables found in each computational graph.
        inputs_var = list(map(graph_inputs, ([var1], [var2])))
        all_vars = [
            set(vars_between(v_i, v_o))
            for v_i, v_o in ((inputs_var[0], [var1]), (inputs_var[1], [var2]))
        ]

        def in_var(x, k):
            # Return True iff `x` is in computation graph of variable `vark`.
            return x in all_vars[k - 1]

        for to_replace, replace_by in givens.items():
            # Map a substitution variable to the computational graphs it
            # belongs to.
            inside = {
                v: [in_var(v, k) for k in (1, 2)] for v in (to_replace, replace_by)
            }
            if (
                inside[to_replace][0]
                and not inside[to_replace][1]
                and inside[replace_by][1]
                and not inside[replace_by][0]
            ):
                # Substitute variable in `var1` by one from `var2`.
                in_xs.append(to_replace)
                in_ys.append(replace_by)
            elif (
                inside[to_replace][1]
                and not inside[to_replace][0]
                and inside[replace_by][0]
                and not inside[replace_by][1]
            ):
                # Substitute variable in `var2` by one from `var1`.
                in_xs.append(replace_by)
                in_ys.append(to_replace)
            else:
                ok = False
                break
        if not ok:
            # We cannot directly use `equal_computations`.
            use_equal_computations = False
    else:
        in_xs = None
        in_ys = None
    if use_equal_computations:
        rval2 = equal_computations(xs=[var1], ys=[var2], in_xs=in_xs, in_ys=in_ys)
        assert rval2 == rval1
    return rval1


def get_clients_at_depth(
    fgraph: FunctionGraph, node: Apply, depth: int
) -> Generator[Apply, None, None]:
    """Yields node clients at given depth."""
    for node in node.outputs:
        if depth > 0:
            for out_node, _ in fgraph.clients[node]:
                if out_node == "output":
                    continue
                yield from get_clients_at_depth(fgraph, out_node, depth - 1)
        else:
            yield node.owner

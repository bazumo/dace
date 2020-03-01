""" This module contains classes and functions that implement the split
    strip-mining transformation."""

import dace
from copy import deepcopy as dcpy
from dace import dtypes, registry, subsets, symbolic
from dace.sdfg import SDFG, SDFGState
from dace.properties import make_properties, Property
from dace.graph import nodes, nxutil
from dace.transformation import pattern_matching
import sympy


def calc_set_image_index(map_idx, map_set, array_idx):
    image = []
    for a_idx in array_idx.indices:
        new_range = [a_idx, a_idx, 1]
        for m_idx, m_range in zip(map_idx, map_set):
            symbol = symbolic.pystr_to_symbolic(m_idx)
            new_range[0] = new_range[0].subs(
                symbol, dace.symbolic.overapproximate(m_range[0]))
            new_range[1] = new_range[1].subs(
                symbol, dace.symbolic.overapproximate(m_range[1]))
        image.append(new_range)
    return subsets.Range(image)


def calc_set_image_range(map_idx, map_set, array_range):
    image = []
    for a_range in array_range:
        new_range = a_range
        for m_idx, m_range in zip(map_idx, map_set):
            symbol = symbolic.pystr_to_symbolic(m_idx)
            new_range = [
                new_range[i].subs(symbol,
                                  dace.symbolic.overapproximate(m_range[i]))
                if dace.symbolic.issymbolic(new_range[i]) else new_range[i]
                for i in range(0, 3)
            ]
        image.append(new_range)
    return subsets.Range(image)


def calc_set_image(map_idx, map_set, array_set):
    if isinstance(array_set, subsets.Range):
        return calc_set_image_range(map_idx, map_set, array_set)
    if isinstance(array_set, subsets.Indices):
        return calc_set_image_index(map_idx, map_set, array_set)


def calc_set_union(set_a, set_b):
    if isinstance(set_a, subsets.Indices) or isinstance(
            set_b, subsets.Indices):
        raise NotImplementedError('Set union with indices is not implemented.')
    if not (isinstance(set_a, subsets.Range)
            and isinstance(set_b, subsets.Range)):
        raise TypeError('Can only compute the union of ranges.')
    if len(set_a) != len(set_b):
        raise ValueError('Range dimensions do not match')
    union = []
    for range_a, range_b in zip(set_a, set_b):
        union.append([
            sympy.Min(range_a[0], range_b[0]),
            sympy.Max(range_a[1], range_b[1]),
            sympy.Min(range_a[2], range_b[2]),
        ])
    return subsets.Range(union)


@registry.autoregister_params(singlestate=True)
@make_properties
class SplitStripMining(pattern_matching.Transformation):
    """ Implements the split strip-mining transformation.

        Split strip-mining takes as input a map dimension and splits it into
        two dimensions. The new dimension iterates over the range of
        the original one with a parameterizable step, called the tile
        size. The original dimension is changed to iterates over the
        range of the tile size, with the same step as before. The difference of
        split strip-mining compared to the simple one, is that it splits out
        the last and potentially imperfect tile to a separate map.
    """

    _map_entry = nodes.MapEntry(nodes.Map("", [], []))

    # Properties
    dim_idx = Property(dtype=int,
                       default=-1,
                       desc="Index of dimension to be strip-mined")
    new_dim_prefix = Property(dtype=str,
                              default="tile",
                              desc="Prefix for new dimension name")
    tile_size = Property(dtype=str,
                         default="64",
                         desc="Tile size of strip-mined dimension")
    # tile_stride = Property(dtype=str,
    #                        default="",
    #                        desc="Stride between two tiles of the "
    #                        "strip-mined dimension")
    # divides_evenly = Property(dtype=bool,
    #                           default=False,
    #                           desc="Tile size divides dimension range evenly?")
    # strided = Property(
    #     dtype=bool,
    #     default=False,
    #     desc="Continuous (false) or strided (true) elements in tile")

    @staticmethod
    def annotates_memlets():
        return True

    @staticmethod
    def expressions():
        return [
            nxutil.node_path_graph(SplitStripMining._map_entry)
            # kStripMining._tasklet, SplitStripMining._map_exit)
        ]

    @staticmethod
    def can_be_applied(graph, candidate, expr_index, sdfg, strict=False):
        return True

    @staticmethod
    def match_to_str(graph, candidate):
        map_entry = graph.nodes()[candidate[SplitStripMining._map_entry]]
        return map_entry.map.label + ': ' + str(map_entry.map.params)

    def apply(self, sdfg):
        graph = sdfg.nodes()[self.state_id]
        # Strip-mine selected dimension.
        _, _, new_map_entry, imperfect_map_entry = self._stripmine(
            sdfg, graph, self.subgraph)
        return new_map_entry, imperfect_map_entry

    # def __init__(self, tag=True):
    def __init__(self, *args, **kwargs):
        self._entry = nodes.EntryNode()
        self._tasklet = nodes.Tasklet('_')
        self._exit = nodes.ExitNode()
        super().__init__(*args, **kwargs)
        # self.tag = tag

    @property
    def entry(self):
        return self._entry

    @property
    def exit(self):
        return self._exit

    @property
    def tasklet(self):
        return self._tasklet

    def print_match_pattern(self, candidate):
        gentry = candidate[self.entry]
        return str(gentry.map.params[-1])

    def modifies_graph(self):
        return True

    def _find_new_dim(self, sdfg: SDFG, state: SDFGState,
                      entry: nodes.MapEntry, prefix: str, target_dim: str):
        """ Finds a variable that is not already defined in scope. """
        stree = state.scope_tree()
        if len(prefix) == 0:
            return target_dim
        candidate = '%s_%s' % (prefix, target_dim)
        index = 1
        while candidate in map(str, stree[entry].defined_vars):
            candidate = '%s%d_%s' % (prefix, index, target_dim)
            index += 1
        return candidate

    def _stripmine(self, sdfg, graph, candidate):

        # Retrieve map entry and exit nodes.
        map_entry = graph.nodes()[candidate[SplitStripMining._map_entry]]
        map_exit = graph.exit_nodes(map_entry)[0]

        # Retrieve transformation properties.
        dim_idx = self.dim_idx
        new_dim_prefix = self.new_dim_prefix
        tile_size = self.tile_size
        # divides_evenly = self.divides_evenly
        # strided = self.strided

        # tile_stride = self.tile_stride
        # if tile_stride is None or len(tile_stride) == 0:
        #     tile_stride = tile_size

        # Retrieve parameter and range of dimension to be strip-mined.
        target_dim = map_entry.map.params[dim_idx]
        td_from, td_to, td_step = map_entry.map.range[dim_idx]

        # Create new map. Replace by cloning map object?
        new_dim = self._find_new_dim(sdfg, graph, map_entry, new_dim_prefix,
                                     target_dim)
        nd_from = 0
        # if symbolic.pystr_to_symbolic(tile_stride) == 1:
        #     nd_to = td_to
        # else:
        nd_to = symbolic.pystr_to_symbolic(
            # '(%s + 1 - %s) // (%s) - 1' %
            'int_floor(%s + 1 - %s, %s) - 1' %
            (symbolic.symstr(td_to), symbolic.symstr(td_from), tile_size))
        nd_step = 1
        new_dim_range = (nd_from, nd_to, nd_step)
        new_map = nodes.Map(new_dim + '_' + map_entry.map.label, [new_dim],
                            subsets.Range([new_dim_range]))
        new_map_entry = nodes.MapEntry(new_map)
        new_map_exit = nodes.MapExit(new_map)

        # Change the range of the selected dimension to iterate over a single
        # tile
        # if strided:
        #     td_from_new = symbolic.pystr_to_symbolic(new_dim)
        #     td_to_new_approx = td_to
        #     td_step = symbolic.pystr_to_symbolic(tile_size)
        # else:
        td_from_perfect = symbolic.pystr_to_symbolic(
            '%s + %s * %s' %
            (symbolic.symstr(td_from), str(new_dim), tile_size))
        td_from_imperfect = symbolic.pystr_to_symbolic(
            # '%s + ((%s + 1 - %s) // (%s)) * %s' %
            '%s + int_floor(%s + 1 - %s, %s) * %s' %
            (symbolic.symstr(td_from), symbolic.symstr(td_to),
             symbolic.symstr(td_from), tile_size, tile_size))
        td_to_perfect = symbolic.pystr_to_symbolic(
            '%s + %s * %s + %s - 1' %
            (symbolic.symstr(td_from), tile_size, str(new_dim), tile_size))
        td_to_imperfect = symbolic.pystr_to_symbolic(
            '%s' % symbolic.symstr(td_to))
        # if divides_evenly or strided:
        #     td_to_new = td_to_new_approx
        # else:
        # td_to_new = dace.symbolic.SymExpr(td_to_new_exact,
        #                                     td_to_new_approx)
        # Special case: If range is 1 and no prefix was specified, skip range
        if td_from_perfect == td_to_perfect and target_dim == new_dim:
            map_entry.map.range = subsets.Range(
                [r for i, r in enumerate(map_entry.map.range) if i != dim_idx])
            map_entry.map.params = [
                p for i, p in enumerate(map_entry.map.params) if i != dim_idx
            ]
            if len(map_entry.map.params) == 0:
                raise ValueError('Strip-mining all dimensions of the map with '
                                 'empty tiles is disallowed')
        else:
            map_entry.map.range[dim_idx] = (td_from_perfect, td_to_perfect,
                                            td_step)

        # Make internal map's schedule to "not parallel"
        new_map.schedule = map_entry.map.schedule
        map_entry.map.schedule = dtypes.ScheduleType.Sequential

        # Redirect edges
        new_map_entry.in_connectors = dcpy(map_entry.in_connectors)
        nxutil.change_edge_dest(graph, map_entry, new_map_entry)
        new_map_exit.out_connectors = dcpy(map_exit.out_connectors)
        nxutil.change_edge_src(graph, map_exit, new_map_exit)

        # Create map for last imperfect tile
        imperfect_range = dcpy(map_entry.map.range.ranges)
        imperfect_range[dim_idx] = (td_from_imperfect, td_to_imperfect, td_step)
        imperfect_map = nodes.Map(
            map_entry.map.label + '_imperfect', dcpy(map_entry.map.params),
            subsets.Range(imperfect_range))
        imperfect_map_entry = nodes.MapEntry(imperfect_map)
        imperfect_map_exit = nodes.MapExit(imperfect_map)
        imperfect_map_entry.in_connectors = dcpy(map_entry.in_connectors)
        imperfect_map_entry.out_connectors = dcpy(map_entry.out_connectors)
        imperfect_map_exit.in_connectors = dcpy(map_exit.in_connectors)
        imperfect_map_exit.out_connectors = dcpy(map_exit.out_connectors)
        graph.add_nodes_from([imperfect_map_entry, imperfect_map_exit])

        # Copy perfect map scope

        scope_dict = dict()
        scope_dict[map_entry] = imperfect_map_entry
        scope_dict[map_exit] = imperfect_map_exit
        for obj in nxutil.traverse_sdfg_scope(graph, map_entry, True):
            if isinstance(obj, nodes.Node) and obj not in scope_dict.keys():
                new_node = dcpy(obj)
                scope_dict[obj] = new_node
                graph.add_node(new_node)
            else:
                src, src_conn, dst, dst_conn, memlet, _ = obj
                if src not in scope_dict.keys():
                    new_node = dcpy(src)
                    scope_dict[src] = new_node
                    graph.add_node(new_node)
                if dst not in scope_dict.keys():
                    new_node = dcpy(dst)
                    scope_dict[dst] = new_node
                    graph.add_node(new_node)
                graph.add_edge(scope_dict[src], src_conn, scope_dict[dst],
                               dst_conn, dcpy(memlet))

        # Create new entry edges
        new_in_edges = dict()
        entry_in_conn = set()
        entry_out_conn = set()
        for _src, src_conn, _dst, _, memlet in graph.out_edges(map_entry):
            if (src_conn is not None
                    and src_conn[:4] == 'OUT_' and not isinstance(
                        sdfg.arrays[memlet.data], dace.data.Scalar)):
                new_subset = calc_set_image(
                    map_entry.map.params,
                    map_entry.map.range,
                    memlet.subset,
                )
                conn = src_conn[4:]
                key = (memlet.data, 'IN_' + conn, 'OUT_' + conn)
                if key in new_in_edges.keys():
                    old_subset = new_in_edges[key].subset
                    new_in_edges[key].subset = calc_set_union(
                        old_subset, new_subset)
                else:
                    entry_in_conn.add('IN_' + conn)
                    entry_out_conn.add('OUT_' + conn)
                    new_memlet = dcpy(memlet)
                    new_memlet.subset = new_subset
                    new_memlet.num_accesses = new_memlet.num_elements()
                    new_in_edges[key] = new_memlet
            else:
                if src_conn is not None and src_conn[:4] == 'OUT_':
                    conn = src_conn[4:]
                    in_conn = 'IN_' + conn
                    out_conn = 'OUT_' + conn
                else:
                    in_conn = src_conn
                    out_conn = src_conn
                if in_conn:
                    entry_in_conn.add(in_conn)
                if out_conn:
                    entry_out_conn.add(out_conn)
                new_in_edges[(memlet.data, in_conn, out_conn)] = dcpy(memlet)
        new_map_entry.out_connectors = entry_out_conn
        map_entry.in_connectors = entry_in_conn
        for (_, in_conn, out_conn), memlet in new_in_edges.items():
            graph.add_edge(new_map_entry, out_conn, map_entry, in_conn, memlet)

        # Create new entry edges (imperfect tile)
        new_in_edges = dict()
        entry_in_conn = set()
        entry_out_conn = set()
        for _src, src_conn, _dst, _, memlet in graph.out_edges(imperfect_map_entry):
            if (src_conn is not None
                    and src_conn[:4] == 'OUT_' and not isinstance(
                        sdfg.arrays[memlet.data], dace.data.Scalar)):
                new_subset = calc_set_image(
                    imperfect_map_entry.map.params,
                    imperfect_map_entry.map.range,
                    memlet.subset,
                )
                conn = src_conn[4:]
                key = (memlet.data, 'IN_' + conn, 'OUT_' + conn)
                if key in new_in_edges.keys():
                    old_subset = new_in_edges[key].subset
                    new_in_edges[key].subset = calc_set_union(
                        old_subset, new_subset)
                else:
                    entry_in_conn.add('IN_' + conn)
                    entry_out_conn.add('OUT_' + conn)
                    new_memlet = dcpy(memlet)
                    new_memlet.subset = new_subset
                    new_memlet.num_accesses = new_memlet.num_elements()
                    new_in_edges[key] = new_memlet
            else:
                if src_conn is not None and src_conn[:4] == 'OUT_':
                    conn = src_conn[4:]
                    in_conn = 'IN_' + conn
                    out_conn = 'OUT_' + conn
                else:
                    in_conn = src_conn
                    out_conn = src_conn
                if in_conn:
                    entry_in_conn.add(in_conn)
                if out_conn:
                    entry_out_conn.add(out_conn)
                new_in_edges[(memlet.data, in_conn, out_conn)] = dcpy(memlet)
        # new_map_entry.out_connectors = entry_out_conn
        imperfect_map_entry.in_connectors = entry_in_conn
        for (_, in_conn, out_conn), memlet in new_in_edges.items():
            for src, src_conn, _, _, scope_mem in graph.in_edges(new_map_entry):
                if scope_mem.data == memlet.data:
                    graph.add_edge(src, src_conn, imperfect_map_entry,
                                   in_conn, memlet)
                    break
        # Add map range symbol edges and connectors
        new_conn = set()
        for src, src_conn, _, dst_conn, mem in graph.in_edges(new_map_entry):
            if dst_conn[:3] != 'IN_':
                new_conn.add(dst_conn)
                graph.add_edge(src, src_conn, imperfect_map_entry,
                               dst_conn, dcpy(mem))
        imperfect_map_entry.in_connectors = imperfect_map_entry.in_connectors.union(new_conn)

        # Create new exit edges
        new_out_edges = dict()
        exit_in_conn = set()
        exit_out_conn = set()
        for _src, _, _dst, dst_conn, memlet in graph.in_edges(map_exit):
            if (dst_conn is not None
                    and dst_conn[:3] == 'IN_' and not isinstance(
                        sdfg.arrays[memlet.data], dace.data.Scalar)):
                new_subset = calc_set_image(
                    map_entry.map.params,
                    map_entry.map.range,
                    memlet.subset,
                )
                conn = dst_conn[3:]
                key = (memlet.data, 'IN_' + conn, 'OUT_' + conn)
                if key in new_out_edges.keys():
                    old_subset = new_out_edges[key].subset
                    new_out_edges[key].subset = calc_set_union(
                        old_subset, new_subset)
                else:
                    exit_in_conn.add('IN_' + conn)
                    exit_out_conn.add('OUT_' + conn)
                    new_memlet = dcpy(memlet)
                    new_memlet.subset = new_subset
                    new_memlet.num_accesses = new_memlet.num_elements()
                    new_out_edges[key] = new_memlet
            else:
                if dst_conn is not None and dst_conn[:3] == 'IN_':
                    conn = dst_conn[3:]
                    in_conn = 'IN_' + conn
                    out_conn = 'OUT_' + conn
                else:
                    in_conn = src_conn
                    out_conn = src_conn
                if in_conn:
                    exit_in_conn.add(in_conn)
                if out_conn:
                    exit_out_conn.add(out_conn)
                new_in_edges[(memlet.data, in_conn, out_conn)] = dcpy(memlet)
        new_map_exit.in_connectors = exit_in_conn
        map_exit.out_connectors = exit_out_conn
        for (_, in_conn, out_conn), memlet in new_out_edges.items():
            graph.add_edge(map_exit, out_conn, new_map_exit, in_conn, memlet)
        
        # Create new exit edges (imperfect tile)
        new_out_edges = dict()
        exit_in_conn = set()
        exit_out_conn = set()
        for _src, _, _dst, dst_conn, memlet in graph.in_edges(imperfect_map_exit):
            if (dst_conn is not None
                    and dst_conn[:3] == 'IN_' and not isinstance(
                        sdfg.arrays[memlet.data], dace.data.Scalar)):
                new_subset = calc_set_image(
                    imperfect_map_entry.map.params,
                    imperfect_map_entry.map.range,
                    memlet.subset,
                )
                conn = dst_conn[3:]
                key = (memlet.data, 'IN_' + conn, 'OUT_' + conn)
                if key in new_out_edges.keys():
                    old_subset = new_out_edges[key].subset
                    new_out_edges[key].subset = calc_set_union(
                        old_subset, new_subset)
                else:
                    exit_in_conn.add('IN_' + conn)
                    exit_out_conn.add('OUT_' + conn)
                    new_memlet = dcpy(memlet)
                    new_memlet.subset = new_subset
                    new_memlet.num_accesses = new_memlet.num_elements()
                    new_out_edges[key] = new_memlet
            else:
                if dst_conn is not None and dst_conn[:3] == 'IN_':
                    conn = dst_conn[3:]
                    in_conn = 'IN_' + conn
                    out_conn = 'OUT_' + conn
                else:
                    in_conn = src_conn
                    out_conn = src_conn
                if in_conn:
                    exit_in_conn.add(in_conn)
                if out_conn:
                    exit_out_conn.add(out_conn)
                new_in_edges[(memlet.data, in_conn, out_conn)] = dcpy(memlet)
        # new_map_exit.in_connectors = exit_in_conn
        imperfect_map_exit.out_connectors = exit_out_conn
        for (_, in_conn, out_conn), memlet in new_out_edges.items():
            for _, _, dst, dst_conn, scope_mem in graph.out_edges(new_map_exit):
                if scope_mem.data == memlet.data:
                    graph.add_edge(imperfect_map_exit, out_conn, dst,
                                   dst_conn, memlet)
                    break

        # Return strip-mined dimension.
        return target_dim, new_dim, new_map_entry, imperfect_map_entry
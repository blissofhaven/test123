"""Lossless canonical mode inputs to private compatibility DTO parameters."""
from __future__ import annotations

from copy import deepcopy
from collections.abc import Mapping
from ..domain.electrical import thaw_json
from ..domain.operating_parameters import (OPERATING_PARAMETERS_KEY,
    operating_parameters_to_dict, validate_operating_parameters)


def attach_operating_parameters(model, net, trace, *, state_ids=None):
    from .legacy_calculation import _legacy_or_native_id
    selected = None if state_ids is None else frozenset(state_ids)
    for state in model.operating_states.values():
        if selected is not None and state.id not in selected:
            continue
        if OPERATING_PARAMETERS_KEY not in state.extensions:
            continue
        params = validate_operating_parameters(model, state)
        raw = operating_parameters_to_dict(params)
        mode_id = _legacy_or_native_id("mode", state.id.value, state.extensions)
        sources = {}
        for eid, values in params.sources.items():
            ids = trace.domain_equipment_to_legacy[eid.value]
            if len(ids) != 1 or ids[0] not in net.branches:
                raise ValueError("Режимный источник не имеет однозначной расчётной ветви.")
            sources[ids[0]] = raw["sources"][eid.value]
        factors = {}
        for eid in params.load_factors:
            ids = trace.domain_equipment_to_legacy[eid.value]
            if len(ids) != 1 or ids[0] not in net.loads:
                raise ValueError("Режимная нагрузка не имеет однозначного расчётного ID.")
            factors[ids[0]] = raw["load_factors"][eid.value]
        currents = {}
        for pid, value in params.working_currents.items():
            port = model.ports[pid]
            node = model.node_for_port(pid)
            if node is None:
                raise ValueError("Рабочий ток задан у неподключённого физического вывода.")
            branch_ids = list(trace.domain_equipment_to_legacy[port.equipment_id.value])
            equipment = model.equipment[port.equipment_id]
            explicit = equipment.extensions.get("rza_calc.ct_port_id")
            slot = equipment.extensions.get("editor_legacy_ct_ports", {}).get("ct_node", {})
            if explicit is None and isinstance(slot, Mapping):
                explicit = slot.get("port_id")
            applicable = []
            for bid in branch_ids:
                branch = net.branches.get(bid)
                if branch is None or not branch.has_protection_point:
                    continue
                candidates = [other.id.value for other in model.ports_of(equipment.id)
                    if (other_node := model.node_for_port(other.id)) is not None
                    and trace.domain_node_to_legacy.get(other_node.id.value) == net.protection_node_id(branch)]
                ct_port = explicit if explicit is not None else candidates[0] if len(candidates) == 1 else None
                if ct_port == pid.value:
                    applicable.append(bid)
            currents[pid.value] = dict(raw["working_currents"][pid.value],
                node_id=trace.domain_node_to_legacy[node.id.value],
                branch_ids=branch_ids, applies_to=applicable)
        net.modes[mode_id].operating_parameters = {
            "version": 1, "sources": sources, "load_factor": raw["load_factor"],
            "load_factors": factors, "working_currents": currents,
            "parallel_operation": params.parallel_operation,
        }


def network_for_mode(net, mode):
    """Return an execution-owned view with this mode's values and provenance.

    The cache lives only on the private execution Network. A value-based key
    also makes direct legacy callers safe after an in-place parameter edit.
    """
    import json
    from ..core.fingerprint import network_fingerprint
    params = mode.operating_parameters
    if not params or not (params.get("sources") or params.get("load_factor") is not None
                          or params.get("load_factors")):
        return net
    # Calculation uses a frozen topology scope; non-frozen callers get a fresh
    # view, avoiding any persistent cache with ambiguous mutation ownership.
    frozen = net.__dict__.get("_frozen_keys") is not None
    stamp = json.dumps(params, sort_keys=True, ensure_ascii=False, allow_nan=False)
    cache = net.__dict__.setdefault("_operating_networks", {}) if frozen else {}
    cached = cache.get(mode.id)
    if cached is not None and cached[0] == stamp:
        return cached[1]
    # Avoid recursively copying previously materialised modes or solver caches.
    from ..calculation.input import NetworkSnapshot
    view = NetworkSnapshot.capture(net).materialize()
    for branch_id, values in params.get("sources", {}).items():
        branch = view.branches[branch_id]
        # A mode-specific leaf overrides its common counterpart independently
        # of dictionary insertion order, which is not part of electrical identity.
        for key, row in sorted(values.items(), key=lambda item: (item[0].startswith("sequence_by_system."), item[0])):
            value = deepcopy(row["value"])
            leaf = key
            if key.startswith("sequence_by_system."):
                _, system, leaf = key.split(".")
                if system != mode.system:
                    continue
            if not hasattr(branch, leaf) or leaf in {"id", "name", "node_from", "node_to"}:
                raise ValueError("Неизвестное режимное поле источника: " + key)
            setattr(branch, leaf, value)
            branch.parameter_provenance[leaf] = dict(row, origin="operating_state")
            # An explicit mode value (including None) wins over equipment
            # common AND max/min fallbacks; no missing input becomes inherited.
            if leaf in {"r2_ohm", "x2_ohm", "r0_ohm", "x0_ohm", "sequence_reference_kv"}:
                current = branch.sequence_by_system.get(mode.system)
                if isinstance(current, dict):
                    current.pop(leaf, None)
    for load in view.loads.values():
        applied = [params.get("load_factor"), params.get("load_factors", {}).get(load.id)]
        for row in applied:
            if row is None:
                continue
            if row.get("confirmation") == "confirmed" and row.get("value") is not None:
                load.p_kw *= row["value"]
            else:
                # Loads do not contribute to the existing fault solver. Retain
                # independent faults while blocking consumption by protection.
                load.parameter_provenance["p_kw"] = dict(row, origin="operating_state")
    cache[mode.id] = stamp, view
    return view


def parallel_source_groups(net, mode):
    """Connected active sources, excluding the fictitious common EMF node."""
    from ..core.model import GRID, SourceBranch, GeneratorBranch
    adjacency = {key: set() for key in net.nodes}
    sources = {}
    for branch in net.active_branches(mode):
        if isinstance(branch, (SourceBranch, GeneratorBranch)):
            sources.setdefault(branch.node_to, []).append(branch.id)
        elif branch.node_from != GRID and branch.node_to != GRID:
            adjacency[branch.node_from].add(branch.node_to)
            adjacency[branch.node_to].add(branch.node_from)
    unseen, groups = set(adjacency), []
    while unseen:
        first = next(iter(unseen))
        pending, members = [first], []
        unseen.remove(first)
        while pending:
            node = pending.pop()
            members.extend(sources.get(node, ()))
            for other in adjacency[node] & unseen:
                unseen.remove(other)
                pending.append(other)
        if len(members) > 1:
            groups.append(tuple(sorted(members)))
    return tuple(sorted(groups))

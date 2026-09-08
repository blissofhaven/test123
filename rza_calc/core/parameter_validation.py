"""Confirm only the explicit inputs consumed by the existing protections.

This module does not select a setting or change a protection zone. Historical
records without provenance keep their previous contract. A newly audited draft
blocks the affected protection, while independent fault calculations remain
available. Scope traversal uses the same topology helpers as the calculators.
"""
from collections.abc import Mapping

from .model import GeneratorBranch, LineBranch, TransformerBranch
from .result import ProtectionResult


_LOAD_KEYS = ("p_kw", "cos_phi", "k_use")
_CAPACITIVE_KEYS = ("capacitive_current_a_per_km", "length_mm", "parallel_count")
_LABELS = {
    "ct_primary_a": "первичный ток ТТ", "ct_secondary_a": "вторичный ток ТТ",
    "ct_port": "сторона установки ТТ", "p_kw": "активная мощность нагрузки",
    "cos_phi": "коэффициент мощности нагрузки", "k_use": "коэффициент использования нагрузки",
    "capacitive_current_a_per_km": "удельный ёмкостный ток", "length_mm": "длина линии",
    "parallel_count": "число параллельных цепей", "i_inrush_ratio": "кратность броска намагничивания",
}


def _problems(obj, keys):
    metadata = getattr(obj, "parameter_provenance", {})
    if not metadata:
        return ()
    if not isinstance(metadata, Mapping):
        return (f"«{obj.name}»: повреждены сведения о проверке исходных параметров.",)
    problems = []
    for key in keys:
        if key not in metadata:
            continue
        row = metadata[key]
        if (not isinstance(row, Mapping) or row.get("confirmation") != "confirmed"
                or not isinstance(row.get("source"), str) or not row["source"].strip()):
            problems.append(f"«{obj.name}»: {_LABELS[key]} ({key}) не подтверждён; "
                            "укажите источник и подтвердите значение в карточке.")
    return tuple(problems)


def _input_index(ctx):
    # engine.run explicitly freezes its input network. Cache only within that
    # one scope; direct calls on a mutable Context always read current records.
    frozen = ctx.net.__dict__.get("_frozen_keys")
    previous = getattr(ctx, "_parameter_input_index", None)
    if frozen is not None and previous is not None and previous[0] is frozen:
        return previous[1]
    loads = {obj.id: problems for obj in ctx.net.loads.values()
             if (problems := _problems(obj, _LOAD_KEYS))}
    inrush = {obj.id: problems for obj in ctx.net.branches.values()
              if isinstance(obj, TransformerBranch) and (problems := _problems(obj, ("i_inrush_ratio",)))}
    capacitive = {obj.id: problems for obj in ctx.net.branches.values()
                  if isinstance(obj, LineBranch) and (problems := _problems(obj, _CAPACITIVE_KEYS))}
    index = loads, inrush, capacitive
    if frozen is not None:
        ctx._parameter_input_index = frozen, index
    return index


def block_unconfirmed_parameters(ctx, br, kind):
    """Return an unresolved result only if a consumed value is an explicit draft."""
    load_inputs, inrush_inputs, capacitive_inputs = _input_index(ctx)
    own_keys = ("ct_port",) if kind == "ОЗЗ" else ("ct_primary_a", "ct_secondary_a", "ct_port")
    own = _problems(br, own_keys)
    relevant = {"МТЗ": load_inputs if not isinstance(br, GeneratorBranch) else {},
                "ТО": inrush_inputs, "ОЗЗ": capacitive_inputs}[kind]
    if not own and not relevant:
        return None
    modes, _, applicability = ctx.protection_modes(br)
    if not modes and not applicability:
        return None
    problems = list(own)
    if applicability:
        problems.extend(applicability)
    net = ctx.net
    for mode in modes:
        found = []
        try:
            if kind == "МТЗ" and load_inputs and not isinstance(br, GeneratorBranch):
                loads = net.downstream_loads(br, mode)
                if not loads:
                    zone, parallel = net.group_zone(br, mode)
                    if parallel > 1:
                        loads = [load for load in net.loads.values()
                                 if load.node in zone and mode.is_available(load.id)]
                found.extend(message for load in loads for message in load_inputs.get(load.id, ()))
            elif kind == "ТО" and inrush_inputs:
                from .protections.to import _transformers_at
                candidates = ([br] if isinstance(br, TransformerBranch) else
                              _transformers_at(ctx, br, mode) if br.prot.to_reach == "behind_transformer" else [])
                for transformer in candidates:
                    source, _, ring = net.orient(transformer, mode)
                    if ring:
                        continue  # the existing inrush calculation refuses before consuming Kbr
                    if transformer.id == br.id:
                        if net.protection_node_id(br) != source:
                            continue  # the energized winding's inrush does not cross this CT
                    elif not ctx.is_radial_through(br, source, mode):
                        continue
                    found.extend(inrush_inputs.get(transformer.id, ()))
            elif kind == "ОЗЗ" and capacitive_inputs:
                from .protections.ozz import galvanic_group
                protection_node = net.protection_node_id(br)
                if net.neutral_mode(net.node(protection_node).u_nom) != "isolated":
                    continue  # that protection method is not applicable
                group = galvanic_group(net, protection_node, mode)
                _, load_end, _ = net.orient(br, mode)
                own_nodes = net.downstream_nodes(br, mode) | {load_end}
                for line in net.active_branches(mode):
                    if (line.id in capacitive_inputs and
                            (line.node_from in group or line.node_to in group or line.id == br.id
                             or line.node_from in own_nodes or line.node_to in own_nodes)):
                        found.extend(capacitive_inputs[line.id])
        except (ValueError, KeyError, TypeError, RuntimeError) as exc:
            found.append("Не удалось определить применимость неподтверждённых данных: " + str(exc))
        problems.extend(f"Режим «{mode.name}»: {message}" for message in found)
    if not problems:
        return None
    problems = list(dict.fromkeys(problems))
    result = ProtectionResult(br.id, br.name, kind)
    result.messages.extend(problems)
    result.record_coverage("Подтверждение исходных данных защиты", 0, len(problems), problems)
    result.recompute_status()
    return result

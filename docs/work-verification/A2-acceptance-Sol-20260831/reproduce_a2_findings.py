"""Read-only A2 acceptance probes; run from project root or pass --project PATH.

No project files or baseline are written. All synthetic cases are in memory.
Exit 1 means the documented A2 defects were reproduced, not a launch error.
This script is outside tests/ and does not change the 783-test suite.
"""
from __future__ import annotations

import argparse
import json
import math
import subprocess
import sys
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", type=Path, default=Path.cwd())
    project = parser.parse_args().project.resolve()
    sys.path.insert(0, str(project))

    from rza_calc.cli import cmd_methodology
    from rza_calc.core.engine import run
    from rza_calc.core.methodology import CONFIRMED, PROJECT, Methodology, MethodologyError
    from rza_calc.core.model import (
        GRID, LineBranch, Load, Mode, Network, Node, SourceBranch,
        Transformer3W, TransformerBranch,
    )
    from rza_calc.core.result import round_to_scale
    from rza_calc.io.project import load

    example = project / "rza_calc/examples/ps_severnaya.json"
    observations: dict[str, object] = {}
    defects: dict[str, bool] = {}

    # F1: JSON null is not a named approver or a source.
    net, meth, _ = load(example)
    meth.data["status"] = PROJECT
    meth.data["approval"] = dict.fromkeys(("approved_by", "approved_on", "object"))
    for path in meth.DOCUMENTED:
        node = meth._node(path)
        target = node if "source" in node else node.get("meta", node)
        target["source_status"] = CONFIRMED
    approval_errors = meth.blocking_errors()
    count = 0
    run_rejection = None
    try:
        result = run(net, meth)
        count = len(list(result.all_results()))
    except (ValueError, KeyError) as exc:
        run_rejection = str(exc)
    typical = Methodology.load()
    typical.data["mtz"]["k_ots"]["source"] = None
    source_errors = typical.blocking_errors()
    observations["F1_null_claim"] = {
        "approval_blocking_errors": approval_errors,
        "results_despite_null_approval": count,
        "run_rejection": run_rejection,
        "source_null_blocking_errors": source_errors,
        "expected": "nonempty validation errors; no approved calculation",
    }
    defects["F1_null_claim"] = (not approval_errors and count > 0) or not source_errors

    # F2: Same graph, only declaration order changes.
    def backup_network(transformer_first: bool) -> Network:
        network = Network("backup declaration-order audit")
        for node_id in ("src", "bus", "b", "end"):
            network.add_node(Node(node_id, node_id, 10))
        network.add_branch(SourceBranch(
            id="S", name="S", node_from=GRID, node_to="src",
            s_kz_max=250, s_kz_min=170,
        ))
        network.add_branch(LineBranch(
            id="F", name="F", node_from="src", node_to="bus",
            length_km=2, r0=.249, x0=.427, ct_ratio=(300, 5),
        ))
        transformer = TransformerBranch(
            id="T", name="T 10/10", node_from="bus", node_to="b",
            s_nom=630, u_hv=10, u_lv=10, uk=5.5,
        )
        bypass = LineBranch(
            id="B", name="B", node_from="bus", node_to="b",
            length_km=1, r0=.249, x0=.427,
        )
        for branch in ([transformer, bypass] if transformer_first else [bypass, transformer]):
            network.add_branch(branch)
        network.add_branch(LineBranch(
            id="E", name="E", node_from="b", node_to="end",
            length_km=4, r0=.249, x0=.427,
        ))
        network.add_load(Load("L", "L", "end", p_kw=300))
        network.add_mode(Mode("max", "max", system="max"))
        return network

    orders = []
    for first in (True, False):
        network = backup_network(first)
        calculated = run(network, Methodology.load())
        backup = calculated.ctx.zone_points(network.branches["F"], network.modes["max"])[1]
        orders.append({
            "transformer_declared_first": first,
            "validation": network.validate(),
            "backup": [point.node_id for point in backup],
            "check_names": [check.name for check in calculated.get("F", "МТЗ").checks],
        })
    observations["F2_backup_order"] = orders
    defects["F2_backup_order"] = orders[0]["backup"] != orders[1]["backup"]

    # F3: Generated HV leg repeats the same known nameplate voltage twice.
    network = Network("3W nameplate inrush audit")
    for node_id, voltage in (("s", 110), ("h", 110), ("m", 35), ("l", 10)):
        network.add_node(Node(node_id, node_id, voltage))
    network.add_branch(SourceBranch(
        id="S", name="S", node_from=GRID, node_to="s",
        s_kz_max=2500, s_kz_min=1700,
    ))
    feeder = LineBranch(
        id="F", name="F", node_from="s", node_to="h",
        length_km=20, r0=.249, x0=.427, ct_ratio=(300, 5),
    )
    feeder.prot.to_reach = "behind_transformer"
    network.add_branch(feeder)
    network.add_transformer3w(Transformer3W(
        id="T", name="T 115/38.5/11", node_hv="h", node_mv="m", node_lv="l",
        s_nom=32000, u_hv=115, u_mv=38.5, u_lv=11,
        uk_hm=10.5, uk_hl=17, uk_ml=7, i_inrush_ratio=8,
    ))
    # Explicit base prevents an unrelated missing-u_avg[115] problem.
    network.nodes["T__star"].calculation_base_kv = 115
    network.add_load(Load("L", "L", "l", p_kw=5000))
    network.add_mode(Mode("max", "max", system="max"))
    meth = Methodology.load()
    calculated = run(network, meth)
    protection = calculated.get("F", "ТО")
    inrush = next(step for step in protection.steps if "броска" in step.what)
    expected_nominal = 32000 / (math.sqrt(3) * 115)
    expected_threshold = meth.k("to.k_ots_inrush") * 8 * expected_nominal
    rounded = round_to_scale(expected_threshold, feeder.ct_k, meth.ct_scale())[0]
    observations["F3_three_winding_voltage"] = {
        "validation": network.validate(), "solver_errors": calculated.ctx.errors,
        "given": inrush.given, "inrush_result": inrush.result,
        "actual_i_calc_A": protection.i_calc,
        "expected_I_nom_A": expected_nominal,
        "expected_inrush_threshold_A": expected_threshold,
        "actual_accepted_A": protection.i_primary,
        "rounded_corrected_inrush_A": rounded,
        "limit": "Both round to 1800 A in this fixture; no final-setting difference is claimed.",
    }
    defects["F3_three_winding_voltage"] = not math.isclose(
        protection.i_calc, expected_threshold, rel_tol=1e-9,
    )

    # F4: Displaying the same old result reads live reference descriptions.
    net, meth, _ = load(example)
    calculated = run(net, meth)
    before = cmd_methodology(calculated)
    meth.data["references"]["ПУЭ"] = "CHANGED_AFTER_CALCULATION"
    after = cmd_methodology(calculated)
    observations["F4_live_references"] = {
        "old_protocol_changed": before != after,
        "new_text_in_old_protocol": "CHANGED_AFTER_CALCULATION" in after,
    }
    defects["F4_live_references"] = before != after

    # F5: The data file can redefine its own allowed implementation variants.
    meth = Methodology.load()
    meth.data["to"]["i_nom_basis"].update(value="garbage", options=["garbage"])
    errors = meth.blocking_errors()
    try:
        basis_value = meth.text("to.i_nom_basis")
    except MethodologyError as exc:
        basis_value = "REJECTED: " + str(exc)
    observations["F5_unknown_basis"] = {
        "blocking_errors": errors, "returned_value": basis_value,
        "expected": "reject a variant not implemented by the calculator",
    }
    defects["F5_unknown_basis"] = not errors

    # F6: Execute the actual CLI to record the OS process return code.
    process = subprocess.run(
        [sys.executable, "-B", "-X", "utf8", "-m", "rza_calc", str(example),
         "feeder", "__does_not_exist__"],
        cwd=project, capture_output=True, text=True, encoding="utf-8",
    )
    observations["F6_unknown_feeder_exit"] = {
        "actual_exit": process.returncode, "expected_exit": 2,
        "message": (process.stdout + process.stderr).strip(),
    }
    defects["F6_unknown_feeder_exit"] = process.returncode != 2

    print(json.dumps({"observations": observations, "reproduced": defects},
                     ensure_ascii=False, indent=2))
    print("REPRODUCED_DEFECT_GROUPS=" + str(sum(defects.values())))
    return 1 if any(defects.values()) else 0


if __name__ == "__main__":
    raise SystemExit(main())

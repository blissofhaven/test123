"""Synthetic, in-memory native proof: thick bus crossing is not a junction."""
from pathlib import Path

script = Path(__file__).with_name("native_cleanup_probe.py")
source = script.read_text(encoding="utf8")
source = source[:source.index("try:\n    # Actual legacy CF1_T")]
exec(compile(source, str(script), "exec"))

try:
    bus = controller.add_electrical_node("ПРОВЕРКА: шина 10 кВ", x=1000, y=2500,
        symbol_key="busbar_horizontal", width=500, height=12, voltage_class_id=U10)
    top = controller.add_equipment("builtin.circuit_breaker", "Независимая связь: начало", x=1000, y=2300,
        voltage_class_by_group={"main": U10})
    bottom = controller.add_equipment("builtin.circuit_breaker", "Независимая связь: конец", x=1080, y=2700,
        voltage_class_by_group={"main": U10})
    left = controller.add_equipment("builtin.circuit_breaker", "Подключён к шине", x=560, y=2500,
        voltage_class_by_group={"main": U10})
    controller.connect_ports(top.port_ids[-1], bottom.port_ids[0],
        first_representation_id=top.representation_id, second_representation_id=bottom.representation_id)
    connected = controller.connect_port_to_node(left.port_ids[-1], bus.node_id,
        node_representation_id=bus.representation_id, target_anchor_key="0.1")
    canvas.refresh()
    canvas.scene.select_representations((bus.representation_id,))
    frame_points(QPointF(580,2300), QPointF(1250,2700), zoom=1.4)
    from rza_calc.gui import editor_scene as scene_module
    captured = {}
    original_builder = scene_module.build_wire_displays
    def recording_builder(wires, *args, **kwargs):
        result = original_builder(wires, *args, **kwargs)
        captured.update(result)
        return result
    scene_module.build_wire_displays = recording_builder
    canvas.scene._refresh_route_bridges()
    scene_module.build_wire_displays = original_builder
    displays = captured
    gaps = {str(key): row.gaps for key,row in displays.items() if row.gaps}
    assert gaps, "Must show a visible no-join gap through the filled band"
    assert controller.model.node_for_port(top.port_ids[-1]).id != bus.node_id
    assert controller.model.node_for_port(left.port_ids[-1]).id == bus.node_id
    assert len(canvas.scene._bus_attachment_handles) > 0
    capture("native-wide-bus-crossing-vs-connection", synthetic_in_memory=True,
        bus=bus.representation_id.value, crossing_is_not_electrical_connection=True, gap_count=sum(len(v) for v in gaps.values()))
    canvas.scene.clearSelection()
    capture("native-wide-bus-crossing-vs-connection-unselected", synthetic_in_memory=True)
    result = {"pid":os.getpid(),"project_saved":False,"source_unchanged":DEMO.read_bytes()==raw,
              "platform":"windows","qt_callback_errors":errors,"frames":frames,"gaps":gaps,
              "real_junction_port":left.port_ids[-1].value,"unrelated_crossing_port":top.port_ids[-1].value}
    (OUT/"native-crossings-probe.json").write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding="utf8")
    print(json.dumps({"frames":len(frames),"gaps":len(gaps),"errors":errors},ensure_ascii=False))
except BaseException:
    (OUT/"native-crossings-failure.json").write_text(json.dumps({"exception":traceback.format_exc(),"errors":errors},ensure_ascii=False,indent=2),encoding="utf8")
    raise
finally:
    window.hide()
    window.deleteLater()
    app.processEvents()

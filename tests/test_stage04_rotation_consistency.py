"""A connected half-turn replaces both leads without changing electrical ends."""
import pytest

from rza_calc.domain.fingerprint import electrical_model_fingerprint
from test_ui_interaction_gui import _app, _controller, _physical_line


def _share_segment(first, second):
    for a, b in zip(first.waypoints, first.waypoints[1:]):
        for c, d in zip(second.waypoints, second.waypoints[1:]):
            if a.y == b.y == c.y == d.y:
                if min(max(a.x,b.x),max(c.x,d.x)) > max(min(a.x,b.x),min(c.x,d.x)):
                    return True
            if a.x == b.x == c.x == d.x:
                if min(max(a.y,b.y),max(c.y,d.y)) > max(min(a.y,b.y),min(c.y,d.y)):
                    return True
    return False


@pytest.mark.parametrize('first,second,wrong_turn', [
    ((400.0,100.0),(0.0,100.0),0),
    ((200.0,300.0),(200.0,-100.0),90),
])
def test_half_turn_replaces_all_old_leads_atomically(first,second,wrong_turn):
    _app()
    controller = _controller('stage04-two-moving-leads')
    line = _physical_line(controller,first,second)
    inserted = controller.insert_recloser(line.section_id,500_000,'Реклоузер',x=200,y=100)
    before_routes = dict(controller.diagram.routes)
    before_representations = dict(controller.diagram.representations)
    ends = {key:(row.start_anchor,row.end_anchor) for key,row in before_routes.items()}
    fingerprint = electrical_model_fingerprint(controller.model)
    history = len(controller.journal)
    controller.rotate_representation(inserted.representation_id,wrong_turn)
    assert electrical_model_fingerprint(controller.model) == fingerprint
    assert {key:(row.start_anchor,row.end_anchor) for key,row in controller.diagram.routes.items()} == ends
    paths = list(controller.diagram.routes.values())
    assert all(not _share_segment(a,b) for i,a in enumerate(paths) for b in paths[i+1:])
    assert len(controller.journal) == history+1
    controller.undo()
    assert dict(controller.diagram.routes) == before_routes
    assert dict(controller.diagram.representations) == before_representations
    assert electrical_model_fingerprint(controller.model) == fingerprint

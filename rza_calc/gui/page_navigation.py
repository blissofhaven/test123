"""Resolve saved inter-sheet links by canonical IDs, never by displayed names."""
from ..domain.diagram import PageId


def linked_page_target(document, representation_id):
    source = document.representations.get(representation_id)
    if source is None:
        raise ValueError("Источник межлистовой ссылки не найден.")
    page_value = source.extensions.get("linked_page_id")
    if not page_value:
        raise ValueError("Связанный лист не найден: у объекта нет ссылки.")
    page_id = PageId(str(page_value))
    if page_id not in document.pages:
        raise ValueError("Связанный лист не найден: возможно, он был удалён.")
    targets = [row for row in document.representations.values()
               if row.page_id == page_id and (
                   (source.electrical_node_id is not None
                    and row.electrical_node_id == source.electrical_node_id)
                   or (source.equipment_id is not None
                       and row.equipment_id == source.equipment_id))]
    if not targets:
        raise ValueError("На связанном листе не найдено изображение этого узла или аппарата.")
    if len(targets) != 1:
        raise ValueError("На связанном листе найдено несколько изображений этого объекта; ссылка неоднозначна.")
    return targets[0]

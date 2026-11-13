import json
import os
import random

from faker import Faker

from idcard.datagen import CANVAS_H, CANVAS_W, generate_dataset, generate_sample
from idcard.labels import CARD_CLASSES


def test_generate_sample_boxes_inside_canvas():
    rng, faker = random.Random(0), Faker("ko_KR")
    for card_type in CARD_CLASSES:
        s = generate_sample(rng, faker, card_type)
        assert s.image.size == (CANVAS_W, CANVAS_H)
        assert s.boxes[0][0] == card_type
        for _, (x0, y0, x1, y1) in s.boxes:
            assert 0 <= x0 < x1 <= CANVAS_W
            assert 0 <= y0 < y1 <= CANVAS_H


def test_field_boxes_inside_card_box():
    rng, faker = random.Random(1), Faker("ko_KR")
    s = generate_sample(rng, faker)
    cx0, cy0, cx1, cy1 = s.boxes[0][1]
    for _, (x0, y0, x1, y1) in s.boxes[1:]:
        assert x0 >= cx0 - 1 and y0 >= cy0 - 1 and x1 <= cx1 + 1 and y1 <= cy1 + 1


def test_rrn_is_not_real_pattern():
    rng, faker = random.Random(2), Faker("ko_KR")
    s = generate_sample(rng, faker, "card_rrc")
    # 성별 자리 0 → 실재할 수 없는 번호
    assert s.texts["id_number"][7] == "0"


def test_generate_dataset_writes_coco(tmp_path):
    stats = generate_dataset(str(tmp_path), n=6, seed=3)
    assert stats["num_images"] == 6
    coco = json.loads((tmp_path / "annotations.json").read_text(encoding="utf-8"))
    assert len(coco["images"]) == 6
    assert all(os.path.exists(tmp_path / "images" / img["file_name"]) for img in coco["images"])
    assert {c["name"] for c in coco["categories"]} >= set(CARD_CLASSES)

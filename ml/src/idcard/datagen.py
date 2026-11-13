"""합성 신분증 데이터 생성기.

실제 신분증 이미지는 개인정보이므로 사용하지 않는다. 여기서는 가상의 이름/주소와
실재할 수 없는 번호 패턴(주민번호 성별자리 0)으로 카드 이미지를 그리고, "SAMPLE"
워터마크를 넣는다. 결과는 COCO 형식(annotations.json) + JPEG 이미지로 저장한다.

  python -m idcard.datagen --out data/train --n 200 --seed 1
"""

from __future__ import annotations

import argparse
import json
import math
import os
import random
from dataclasses import dataclass, field

import numpy as np
from faker import Faker
from PIL import Image, ImageDraw, ImageEnhance, ImageFilter

from .fonts import load_font
from .labels import CARD_CLASSES, CLASS_TO_ID, CLASSES

CARD_W, CARD_H = 856, 540  # ISO/IEC 7810 ID-1 비율
CANVAS_W, CANVAS_H = 1024, 768

SURNAMES = [("김", "KIM"), ("이", "LEE"), ("박", "PARK"), ("최", "CHOI"), ("정", "JUNG"),
            ("강", "KANG"), ("조", "CHO"), ("윤", "YOON"), ("장", "JANG"), ("임", "LIM"),
            ("한", "HAN"), ("오", "OH"), ("서", "SEO"), ("신", "SHIN"), ("권", "KWON")]
GIVEN = [("민", "MIN"), ("준", "JUN"), ("서", "SEO"), ("연", "YEON"), ("지", "JI"), ("우", "WOO"),
         ("하", "HA"), ("윤", "YUN"), ("도", "DO"), ("현", "HYEON"), ("수", "SU"), ("아", "A"),
         ("예", "YE"), ("은", "EUN"), ("진", "JIN"), ("호", "HO"), ("영", "YEONG"), ("희", "HUI"),
         ("성", "SEONG"), ("훈", "HUN"), ("유", "YU"), ("나", "NA"), ("빈", "BIN"), ("태", "TAE")]
MONTHS_EN = ["JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"]
ISSUERS = ["서울특별시 종로구청장", "부산광역시 해운대구청장", "경기도 성남시 분당구청장",
           "대전광역시 유성구청장", "인천광역시 연수구청장", "광주광역시 북구청장"]
POLICE = ["서울경찰청장", "경기남부경찰청장", "부산경찰청장", "대구경찰청장", "인천경찰청장"]


@dataclass
class Sample:
    image: Image.Image
    boxes: list[tuple[str, tuple[float, float, float, float]]]  # (class, xyxy)
    card_type: str
    texts: dict[str, str] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# 가상 개인정보
# ---------------------------------------------------------------------------
def _fake_person(rng: random.Random, faker: Faker) -> dict[str, str]:
    sur_ko, sur_en = rng.choice(SURNAMES)
    g1, g2 = rng.choice(GIVEN), rng.choice(GIVEN)
    name_ko = sur_ko + g1[0] + g2[0]
    name_en = f"{sur_en} {g1[1]}{g2[1]}"
    yy, mm, dd = rng.randint(50, 99), rng.randint(1, 12), rng.randint(1, 28)
    # 성별 자리를 0으로 두어 실재할 수 없는 번호로 만든다.
    rrn = f"{yy:02d}{mm:02d}{dd:02d}-0{rng.randint(0, 999999):06d}"
    address = faker.address().replace("\n", " ")
    iy, im, idd = rng.randint(2015, 2025), rng.randint(1, 12), rng.randint(1, 28)
    return {
        "name_ko": name_ko,
        "name_en": name_en,
        "rrn": rrn,
        "address": address,
        "issue": (iy, im, idd),
        "license_no": f"{rng.randint(11, 28)}-{rng.randint(10, 25)}-{rng.randint(0, 999999):06d}-{rng.randint(10, 99)}",
        "passport_no": f"M{rng.randint(0, 99999999):08d}",
    }


def _split_address(addr: str, max_chars: int = 22) -> list[str]:
    words = addr.split()
    lines, cur = [], ""
    for w in words:
        if cur and len(cur) + 1 + len(w) > max_chars and len(lines) == 0:
            lines.append(cur)
            cur = w
        else:
            cur = f"{cur} {w}".strip()
    lines.append(cur)
    return lines[:2]


# ---------------------------------------------------------------------------
# 카드 그리기
# ---------------------------------------------------------------------------
class _Card:
    def __init__(self, rng: random.Random, base_rgb: tuple[int, int, int]):
        self.rng = rng
        self.img = Image.new("RGB", (CARD_W, CARD_H), base_rgb)
        self.draw = ImageDraw.Draw(self.img)
        self.boxes: list[tuple[str, tuple[float, float, float, float]]] = []
        self._background_pattern(base_rgb)

    def _background_pattern(self, base: tuple[int, int, int]) -> None:
        rng = self.rng
        for _ in range(rng.randint(8, 20)):
            c = tuple(max(0, min(255, v + rng.randint(-18, 18))) for v in base)
            y = rng.randint(0, CARD_H)
            self.draw.line([(0, y), (CARD_W, y + rng.randint(-80, 80))], fill=c, width=rng.randint(1, 3))
        for _ in range(rng.randint(2, 5)):
            c = tuple(max(0, min(255, v - rng.randint(5, 20))) for v in base)
            x, y, r = rng.randint(0, CARD_W), rng.randint(0, CARD_H), rng.randint(40, 160)
            self.draw.ellipse([x - r, y - r, x + r, y + r], outline=c, width=2)

    def text(self, xy, text, size, cls: str | None = None, fill=(25, 25, 30), pad: int = 4):
        font = load_font(size)
        jitter = (self.rng.randint(-6, 6), self.rng.randint(-4, 4))
        pos = (xy[0] + jitter[0], xy[1] + jitter[1])
        self.draw.text(pos, text, font=font, fill=fill)
        x0, y0, x1, y1 = self.draw.textbbox(pos, text, font=font)
        box = (x0 - pad, y0 - pad, x1 + pad, y1 + pad)
        if cls:
            self.boxes.append((cls, box))
        return box

    def multiline(self, xy, lines, size, cls: str, gap: int = 8):
        boxes = []
        x, y = xy
        for line in lines:
            boxes.append(self.text((x, y), line, size))
            y += size + gap
        x0 = min(b[0] for b in boxes)
        y0 = min(b[1] for b in boxes)
        x1 = max(b[2] for b in boxes)
        y1 = max(b[3] for b in boxes)
        self.boxes.append((cls, (x0, y0, x1, y1)))

    def photo(self, box):
        rng = self.rng
        x0, y0, x1, y1 = box
        bg = tuple(rng.randint(170, 230) for _ in range(3))
        self.draw.rectangle(box, fill=bg, outline=(120, 120, 120), width=2)
        cx, w, h = (x0 + x1) / 2, x1 - x0, y1 - y0
        skin = (rng.randint(150, 230), rng.randint(120, 190), rng.randint(100, 160))
        cloth = tuple(rng.randint(20, 110) for _ in range(3))
        self.draw.ellipse([cx - w * 0.22, y0 + h * 0.15, cx + w * 0.22, y0 + h * 0.55], fill=skin)
        self.draw.pieslice([x0 + w * 0.08, y0 + h * 0.6, x1 - w * 0.08, y1 + h * 0.45], 180, 360, fill=cloth)
        self.boxes.append(("field_photo", tuple(map(float, box))))

    def watermark(self):
        layer = Image.new("RGBA", (CARD_W, CARD_H), (0, 0, 0, 0))
        d = ImageDraw.Draw(layer)
        d.text((CARD_W * 0.28, CARD_H * 0.38), "SAMPLE", font=load_font(110), fill=(220, 40, 40, 45))
        layer = layer.rotate(self.rng.uniform(15, 30), resample=Image.BICUBIC)
        self.img = Image.alpha_composite(self.img.convert("RGBA"), layer).convert("RGB")


def _draw_rrc(rng, p) -> tuple[_Card, dict]:
    card = _Card(rng, (rng.randint(232, 250), rng.randint(236, 250), rng.randint(240, 252)))
    card.text((250, 34), "주민등록증", 46, fill=(20, 40, 90))
    card.text((60, 140), f"{p['name_ko']}", 42, "field_name")
    card.text((60, 205), p["rrn"], 36, "field_id_number")
    addr = _split_address(p["address"])
    card.multiline((60, 272), addr, 26, "field_address")
    y, m, d = p["issue"]
    card.text((300, 418), f"{y}. {m}. {d}.", 28, "field_issue_date")
    card.text((250, 466), rng.choice(ISSUERS), 30)
    card.photo((610, 130, 800, 380))
    return card, {"name": p["name_ko"], "id_number": p["rrn"], "address": " ".join(addr),
                  "issue_date": f"{y}. {m}. {d}."}


def _draw_driver(rng, p) -> tuple[_Card, dict]:
    card = _Card(rng, (rng.randint(225, 245), rng.randint(238, 252), rng.randint(225, 242)))
    card.text((230, 26), "자동차운전면허증", 40, fill=(30, 70, 40))
    card.text((240, 76), "Driver's License", 22, fill=(60, 90, 60))
    card.text((60, 118), p["license_no"], 30)
    card.photo((50, 170, 230, 400))
    card.text((265, 172), p["name_ko"], 36, "field_name")
    card.text((265, 226), p["rrn"], 30, "field_id_number")
    addr = _split_address(p["address"], 20)
    card.multiline((265, 282), addr, 24, "field_address")
    y, m, d = p["issue"]
    card.text((265, 392), f"{y}.{m:02d}.{d:02d}", 26, "field_issue_date")
    card.text((265, 432), f"적성검사기간 {y + 10}.01.01~{y + 10}.12.31", 20)
    card.text((560, 478), rng.choice(POLICE), 28)
    return card, {"name": p["name_ko"], "id_number": p["rrn"], "address": " ".join(addr),
                  "issue_date": f"{y}.{m:02d}.{d:02d}"}


def _draw_passport(rng, p) -> tuple[_Card, dict]:
    card = _Card(rng, (rng.randint(228, 245), rng.randint(225, 240), rng.randint(235, 250)))
    card.text((40, 22), "대한민국 REPUBLIC OF KOREA", 26, fill=(60, 30, 80))
    card.text((560, 22), "여권 PASSPORT", 26, fill=(60, 30, 80))
    card.photo((40, 100, 240, 360))
    card.text((280, 92), "여권번호/Passport No.", 16, fill=(90, 90, 90))
    card.text((280, 114), p["passport_no"], 32, "field_id_number")
    card.text((280, 170), "성명/Name", 16, fill=(90, 90, 90))
    card.text((280, 192), p["name_en"], 28)
    card.text((280, 232), p["name_ko"], 32, "field_name")
    card.text((280, 290), "발급일/Date of issue", 16, fill=(90, 90, 90))
    y, m, d = p["issue"]
    card.text((280, 312), f"{d:02d} {MONTHS_EN[m - 1]} {y}", 28, "field_issue_date")
    surname, given = p["name_en"].split(" ", 1)
    mrz1 = f"PMKOR{surname}<<{given}".ljust(44, "<")[:44]
    mrz2 = f"{p['passport_no']}<KOR{p['rrn'][:6]}<<<<<<<<<<<<<<<<<<<".ljust(44, "<")[:44]
    card.text((40, 420), mrz1, 24)
    card.text((40, 462), mrz2, 24)
    return card, {"name": p["name_ko"], "id_number": p["passport_no"],
                  "issue_date": f"{d:02d} {MONTHS_EN[m - 1]} {y}"}


DRAWERS = {"card_rrc": _draw_rrc, "card_driver": _draw_driver, "card_passport": _draw_passport}


# ---------------------------------------------------------------------------
# 배경 합성 + 기하/색상 증강
# ---------------------------------------------------------------------------
def _background(rng: random.Random) -> Image.Image:
    base = tuple(rng.randint(40, 200) for _ in range(3))
    bg = Image.new("RGB", (CANVAS_W, CANVAS_H), base)
    d = ImageDraw.Draw(bg)
    for _ in range(rng.randint(5, 15)):
        c = tuple(rng.randint(0, 255) for _ in range(3))
        x0, y0 = rng.randint(0, CANVAS_W), rng.randint(0, CANVAS_H)
        x1, y1 = x0 + rng.randint(30, 400), y0 + rng.randint(30, 300)
        if rng.random() < 0.5:
            d.rectangle([x0, y0, x1, y1], fill=c)
        else:
            d.ellipse([x0, y0, x1, y1], fill=c)
    return bg.filter(ImageFilter.GaussianBlur(rng.uniform(2, 6)))


def _rotate_point(x, y, cx, cy, ncx, ncy, theta):
    """PIL rotate(angle, expand=True)와 같은 변환 (반시계 방향, y축 아래 방향)."""
    dx, dy = x - cx, y - cy
    return (dx * math.cos(theta) + dy * math.sin(theta) + ncx,
            -dx * math.sin(theta) + dy * math.cos(theta) + ncy)


def _transform_box(box, scale, theta, cx, cy, ncx, ncy, ox, oy):
    x0, y0, x1, y1 = (v * scale for v in box)
    pts = [_rotate_point(x, y, cx, cy, ncx, ncy, theta) for x, y in ((x0, y0), (x1, y0), (x1, y1), (x0, y1))]
    xs, ys = [p[0] + ox for p in pts], [p[1] + oy for p in pts]
    return (max(0.0, min(xs)), max(0.0, min(ys)), min(float(CANVAS_W), max(xs)), min(float(CANVAS_H), max(ys)))


def generate_sample(rng: random.Random, faker: Faker, card_type: str | None = None) -> Sample:
    card_type = card_type or rng.choice(CARD_CLASSES)
    person = _fake_person(rng, faker)
    card, texts = DRAWERS[card_type](rng, person)
    card.watermark()

    scale = rng.uniform(0.55, 0.88) * CANVAS_W / CARD_W
    sw, sh = int(CARD_W * scale), int(CARD_H * scale)
    card_rgba = card.img.resize((sw, sh), Image.BICUBIC).convert("RGBA")
    angle = rng.uniform(-12, 12)
    rotated = card_rgba.rotate(angle, resample=Image.BICUBIC, expand=True)
    rw, rh = rotated.size

    # 캔버스 안에 완전히 들어가도록 스케일을 줄인다.
    if rw > CANVAS_W or rh > CANVAS_H:
        f = min(CANVAS_W / rw, CANVAS_H / rh) * 0.98
        scale *= f
        sw, sh = int(CARD_W * scale), int(CARD_H * scale)
        card_rgba = card.img.resize((sw, sh), Image.BICUBIC).convert("RGBA")
        rotated = card_rgba.rotate(angle, resample=Image.BICUBIC, expand=True)
        rw, rh = rotated.size

    ox, oy = rng.randint(0, CANVAS_W - rw), rng.randint(0, CANVAS_H - rh)
    canvas = _background(rng)
    canvas.paste(rotated, (ox, oy), rotated)

    theta = math.radians(angle)
    cx, cy, ncx, ncy = sw / 2, sh / 2, rw / 2, rh / 2
    boxes = [(card_type, _transform_box((0, 0, CARD_W, CARD_H), scale, theta, cx, cy, ncx, ncy, ox, oy))]
    for cls, box in card.boxes:
        boxes.append((cls, _transform_box(box, scale, theta, cx, cy, ncx, ncy, ox, oy)))

    # 색상/품질 증강
    canvas = ImageEnhance.Brightness(canvas).enhance(rng.uniform(0.7, 1.3))
    canvas = ImageEnhance.Contrast(canvas).enhance(rng.uniform(0.7, 1.3))
    if rng.random() < 0.3:
        canvas = canvas.filter(ImageFilter.GaussianBlur(rng.uniform(0.3, 1.2)))
    if rng.random() < 0.3:
        arr = np.asarray(canvas).astype(np.int16)
        noise = np.random.default_rng(rng.randint(0, 2**31)).normal(0, rng.uniform(3, 10), arr.shape)
        canvas = Image.fromarray(np.clip(arr + noise, 0, 255).astype(np.uint8))

    return Sample(image=canvas, boxes=boxes, card_type=card_type, texts=texts)


def generate_dataset(out_dir: str, n: int, seed: int = 42) -> dict:
    """out_dir/images/*.jpg, out_dir/annotations.json(COCO) 생성 후 통계를 반환한다."""
    os.makedirs(os.path.join(out_dir, "images"), exist_ok=True)
    rng = random.Random(seed)
    faker = Faker("ko_KR")
    faker.seed_instance(seed)

    coco = {
        "info": {"description": "Synthetic Korean ID cards (SAMPLE, not real)", "seed": seed},
        "categories": [{"id": i, "name": c} for i, c in enumerate(CLASSES) if i > 0],
        "images": [],
        "annotations": [],
    }
    ann_id = 1
    for i in range(n):
        s = generate_sample(rng, faker)
        fname = f"{i:06d}.jpg"
        s.image.save(os.path.join(out_dir, "images", fname), quality=rng.randint(60, 95))
        coco["images"].append({"id": i, "file_name": fname, "width": CANVAS_W, "height": CANVAS_H,
                               "card_type": s.card_type, "texts": s.texts})
        for cls, (x0, y0, x1, y1) in s.boxes:
            w, h = x1 - x0, y1 - y0
            if w < 2 or h < 2:
                continue
            coco["annotations"].append({
                "id": ann_id, "image_id": i, "category_id": CLASS_TO_ID[cls],
                "bbox": [round(x0, 2), round(y0, 2), round(w, 2), round(h, 2)],
                "area": round(w * h, 2), "iscrowd": 0,
            })
            ann_id += 1

    with open(os.path.join(out_dir, "annotations.json"), "w", encoding="utf-8") as f:
        json.dump(coco, f, ensure_ascii=False)
    return dataset_stats(coco)


def dataset_stats(coco: dict) -> dict:
    by_cls: dict[str, int] = {c: 0 for c in CLASSES[1:]}
    for a in coco["annotations"]:
        by_cls[CLASSES[a["category_id"]]] += 1
    by_card: dict[str, int] = {c: 0 for c in CARD_CLASSES}
    for img in coco["images"]:
        by_card[img["card_type"]] = by_card.get(img["card_type"], 0) + 1
    return {"num_images": len(coco["images"]), "num_annotations": len(coco["annotations"]),
            "annotations_per_class": by_cls, "images_per_card_type": by_card}


def main() -> None:
    ap = argparse.ArgumentParser(description="합성 신분증 데이터 생성")
    ap.add_argument("--out", required=True)
    ap.add_argument("--n", type=int, default=100)
    ap.add_argument("--seed", type=int, default=42)
    args = ap.parse_args()
    stats = generate_dataset(args.out, args.n, args.seed)
    print(json.dumps(stats, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()

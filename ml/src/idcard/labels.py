"""검출 클래스 정의. 인덱스 0은 torchvision 검출 모델의 배경 클래스."""

CLASSES = [
    "__background__",
    "card_rrc",  # 주민등록증
    "card_driver",  # 운전면허증
    "card_passport",  # 여권
    "field_name",
    "field_id_number",
    "field_address",
    "field_issue_date",
    "field_photo",
]

NUM_CLASSES = len(CLASSES)
CLASS_TO_ID = {name: i for i, name in enumerate(CLASSES)}

CARD_CLASSES = ["card_rrc", "card_driver", "card_passport"]
CARD_CLASS_IDS = [CLASS_TO_ID[c] for c in CARD_CLASSES]

CARD_TYPE_KO = {
    "card_rrc": "주민등록증",
    "card_driver": "운전면허증",
    "card_passport": "여권",
}

# OCR 대상 필드 (사진 영역은 OCR 하지 않는다)
TEXT_FIELDS = ["field_name", "field_id_number", "field_address", "field_issue_date"]

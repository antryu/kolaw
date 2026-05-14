"""
laws_config.py — 5 법령 PoC 확장 정의 (R&D Track #2).

의장 default lock 2026-05-06 #buddy ⑥:
- 약사법
- 민법
- 형법
- 근로기준법
- 자본시장과금융투자업에관한법률 (정식, 약칭 자본시장법)

각 법령:
- name_id    : tree id prefix + 파일명 안전 ascii
- corpus_dir : ~/Thairon/legalize-kr/kr/<법령명>/  의 디렉토리명 (한글 OK)
- sources    : 존재하는 (법률, 시행령, 시행규칙) sub-md
- questions  : 5 질문 (PoC 1차 패턴 + 도메인 특화)

* 민법·형법은 단일 법률 (시행령/시행규칙 별도 X) — sources 1개만
"""

from __future__ import annotations

from pathlib import Path

CORPUS_ROOT = Path.home() / "Thairon" / "legalize-kr" / "kr"


def _existing_sources(corpus_dir: str) -> list[tuple[str, Path]]:
    """corpus 디렉토리 안에 실제 존재하는 source md 만 list.

    legalize-kr 일부 법령은 `법률.md` 가 stub (구판 1997 stub 630B 등) 이고
    실제 본문은 `법률(법률).md` 형태로 들어있음 (예: 근로기준법).
    더 큰 파일을 진짜 본문으로 채택.
    """
    base = CORPUS_ROOT / corpus_dir
    # 법률 후보 — stub vs full 비교, 가장 큰 것 채택
    bub_candidates = [base / "법률.md", base / "법률(법률).md"]
    bub_existing = [p for p in bub_candidates if p.exists()]
    bub_chosen = max(bub_existing, key=lambda p: p.stat().st_size) if bub_existing else None

    out: list[tuple[str, Path]] = []
    if bub_chosen is not None:
        out.append(("법률", bub_chosen))
    for label, fname in [("시행령", "시행령.md"), ("시행규칙", "시행규칙.md")]:
        p = base / fname
        if p.exists():
            out.append((label, p))
    return out


LAWS = [
    {
        "name_id": "yaksabub",  # 약사법
        "display": "약사법",
        "corpus_dir": "약사법",
        "questions": [
            {
                "id": "Q1",
                "question": "약사법상 의약품 판매업의 종류와 등록 요건은?",
                "expect_keywords": ["약국", "한약업사", "의약품도매상", "허가", "등록", "약사", "한약사"],
                "ground_truth_articles": ["약사법 제20조", "약사법 제45조"],
            },
            {
                "id": "Q2",
                "question": "의약품 광고 위반 시 처벌 수위는?",
                "expect_keywords": ["1년", "2년", "3년", "벌금", "징역", "500만원", "1천만원", "3천만원"],
                "ground_truth_articles": ["약사법 제93조", "약사법 제94조", "약사법 제95조", "약사법 제97조"],
            },
            {
                "id": "Q3",
                "question": "전문의약품과 일반의약품 분류 기준은?",
                "expect_keywords": ["전문의약품", "일반의약품", "분류", "안전성", "유효성", "보건복지부장관"],
                "ground_truth_articles": ["약사법 제2조", "약사법 제50조"],
            },
            {
                "id": "Q4",
                "question": "약사 면허 결격사유는?",
                "expect_keywords": ["정신질환", "마약", "금치산", "한정치산", "피성년후견인", "결격"],
                "ground_truth_articles": ["약사법 제5조"],
            },
            {
                "id": "Q5",
                "question": "약사법 시행령·시행규칙 어디에 위임되어 있나?",
                "expect_keywords": ["시행령", "시행규칙", "보건복지부령", "대통령령", "총리령"],
                "ground_truth_articles": ["약사법 시행령", "약사법 시행규칙"],
            },
        ],
    },
    {
        "name_id": "minbub",  # 민법
        "display": "민법",
        "corpus_dir": "민법",
        "questions": [
            {
                "id": "Q1",
                "question": "민법상 계약 성립 요건은?",
                "expect_keywords": ["청약", "승낙", "의사표시", "합의", "계약"],
                "ground_truth_articles": ["민법 제527조", "민법 제534조", "민법 제535조"],
            },
            {
                "id": "Q2",
                "question": "불법행위 손해배상 청구 요건은?",
                "expect_keywords": ["고의", "과실", "위법", "손해", "인과관계", "배상"],
                "ground_truth_articles": ["민법 제750조", "민법 제751조", "민법 제763조"],
            },
            {
                "id": "Q3",
                "question": "소유권 취득시효 기간은?",
                "expect_keywords": ["20년", "10년", "점유", "취득시효", "선의", "무과실"],
                "ground_truth_articles": ["민법 제245조", "민법 제246조"],
            },
            {
                "id": "Q4",
                "question": "혼인의 효력은 언제 발생하나?",
                "expect_keywords": ["혼인신고", "효력", "성립", "동의"],
                "ground_truth_articles": ["민법 제812조", "민법 제815조"],
            },
            {
                "id": "Q5",
                "question": "유언의 방식 종류는?",
                "expect_keywords": ["자필증서", "공정증서", "비밀증서", "녹음", "구수증서", "유언"],
                "ground_truth_articles": ["민법 제1065조", "민법 제1066조", "민법 제1067조", "민법 제1068조", "민법 제1069조", "민법 제1070조"],
            },
        ],
    },
    {
        "name_id": "hyungbub",  # 형법
        "display": "형법",
        "corpus_dir": "형법",
        "questions": [
            {
                "id": "Q1",
                "question": "사기죄의 구성요건과 형량은?",
                "expect_keywords": ["사기", "기망", "10년", "이하", "징역", "벌금", "재산상", "이익"],
                "ground_truth_articles": ["형법 제347조"],
            },
            {
                "id": "Q2",
                "question": "공소시효는 형의 종류별로 어떻게 다른가?",
                "expect_keywords": ["사형", "무기", "10년", "5년", "3년", "공소시효"],
                "ground_truth_articles": ["형법 제249조"],
            },
            {
                "id": "Q3",
                "question": "정당방위 성립 요건은?",
                "expect_keywords": ["정당방위", "현재", "부당한", "침해", "방위", "상당한", "이유"],
                "ground_truth_articles": ["형법 제21조"],
            },
            {
                "id": "Q4",
                "question": "미수범 처벌 규정과 감경 사유는?",
                "expect_keywords": ["미수", "예비", "음모", "감경", "처벌"],
                "ground_truth_articles": ["형법 제25조", "형법 제26조", "형법 제27조", "형법 제28조"],
            },
            {
                "id": "Q5",
                "question": "횡령죄와 배임죄의 차이는?",
                "expect_keywords": ["횡령", "배임", "보관", "임무", "위배", "재산상", "이익"],
                "ground_truth_articles": ["형법 제355조", "형법 제356조", "형법 제357조"],
            },
        ],
    },
    {
        "name_id": "labor",  # 근로기준법
        "display": "근로기준법",
        "corpus_dir": "근로기준법",
        "questions": [
            {
                "id": "Q1",
                "question": "법정근로시간과 연장근로 한도는?",
                "expect_keywords": ["40시간", "8시간", "12시간", "연장근로", "법정근로시간", "주"],
                "ground_truth_articles": ["근로기준법 제50조", "근로기준법 제53조"],
            },
            {
                "id": "Q2",
                "question": "임금체불 시 사용자 처벌은?",
                "expect_keywords": ["3년", "이하", "징역", "벌금", "3천만원", "임금"],
                "ground_truth_articles": ["근로기준법 제109조", "근로기준법 제43조"],
            },
            {
                "id": "Q3",
                "question": "해고 제한 사유와 절차는?",
                "expect_keywords": ["정당한", "이유", "30일", "해고예고", "서면", "통지"],
                "ground_truth_articles": ["근로기준법 제23조", "근로기준법 제26조", "근로기준법 제27조"],
            },
            {
                "id": "Q4",
                "question": "연차유급휴가는 어떻게 발생하고 일수는?",
                "expect_keywords": ["1년", "80%", "15일", "연차", "유급휴가", "출근율"],
                "ground_truth_articles": ["근로기준법 제60조"],
            },
            {
                "id": "Q5",
                "question": "사업장 적용 범위 (5인 미만 등)?",
                "expect_keywords": ["5인 이상", "5인 미만", "상시", "근로자", "적용", "시행령"],
                "ground_truth_articles": ["근로기준법 제11조"],
            },
        ],
    },
    {
        "name_id": "jabonsijang",  # 자본시장과금융투자업에관한법률
        "display": "자본시장과금융투자업에관한법률",
        "corpus_dir": "자본시장과금융투자업에관한법률",
        "questions": [
            {
                "id": "Q1",
                "question": "자본시장법상 공시 의무 (정기·수시) 는?",
                "expect_keywords": ["사업보고서", "반기보고서", "분기보고서", "주요사항보고서", "공시", "금융위원회"],
                "ground_truth_articles": ["자본시장과금융투자업에관한법률 제159조", "자본시장과금융투자업에관한법률 제160조", "자본시장과금융투자업에관한법률 제161조"],
            },
            {
                "id": "Q2",
                "question": "내부자거래 금지와 처벌은?",
                "expect_keywords": ["내부자", "미공개중요정보", "10년", "이하", "징역", "벌금", "5억원"],
                "ground_truth_articles": ["자본시장과금융투자업에관한법률 제174조", "자본시장과금융투자업에관한법률 제443조"],
            },
            {
                "id": "Q3",
                "question": "시세조종 금지 행위 유형은?",
                "expect_keywords": ["시세조종", "통정매매", "가장매매", "허위표시", "변동조작", "안정조작"],
                "ground_truth_articles": ["자본시장과금융투자업에관한법률 제176조"],
            },
            {
                "id": "Q4",
                "question": "금융투자업 인가 종류와 자기자본 요건은?",
                "expect_keywords": ["투자매매업", "투자중개업", "집합투자업", "투자자문업", "투자일임업", "신탁업", "인가", "자기자본"],
                "ground_truth_articles": ["자본시장과금융투자업에관한법률 제12조", "자본시장과금융투자업에관한법률 제15조"],
            },
            {
                "id": "Q5",
                "question": "과징금 부과 대상과 산정 방식은?",
                "expect_keywords": ["과징금", "금융위원회", "위반", "산정", "부과", "20억", "공시"],
                "ground_truth_articles": ["자본시장과금융투자업에관한법률 제429조", "자본시장과금융투자업에관한법률 제430조"],
            },
        ],
    },
]


def get_law(name_id: str) -> dict | None:
    for law in LAWS:
        if law["name_id"] == name_id:
            return law
    return None


def all_law_ids() -> list[str]:
    return [law["name_id"] for law in LAWS]


def sources_for(name_id: str) -> list[tuple[str, Path]]:
    law = get_law(name_id)
    if law is None:
        return []
    return _existing_sources(law["corpus_dir"])


if __name__ == "__main__":
    # quick sanity
    for law in LAWS:
        srcs = _existing_sources(law["corpus_dir"])
        print(f"{law['name_id']:12s} {law['display']:30s} sources={len(srcs)}")
        for label, path in srcs:
            size = path.stat().st_size if path.exists() else 0
            print(f"  - {label:8s} {size:>10,} B  {path}")

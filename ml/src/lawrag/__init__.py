"""개인정보 보호법 Q&A RAG 실습 패키지 (6주차).

pdfparse : 국가법령정보센터 PDF → 조문(Article) 목록
chunk    : 조문 → 검색용 청크(Chunk)
embed    : bge-m3 dense 임베딩 (CPU)
store    : pgvector 저장소 (색인 버전 · 승격 · 검색)
retrieve : 질문 → 근거 청크 top-k (조문 번호 직접 매칭 + 벡터 검색)
prepare  : 로컬 CLI — PDF → articles.jsonl, chunks.jsonl, report.md
index    : 로컬 CLI — 색인 적재 · 승격 · 검색 · 버전 목록
"""

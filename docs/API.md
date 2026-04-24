# kolaw API Reference

Base URL: `http://localhost:8100`

## GET /health

Returns service status and data source availability.

**Response 200**
```json
{
  "status": "ok",
  "version": "0.1.0",
  "data_sources": [
    {"name": "legalize-kr", "status": "ok", "detail": "2303 laws available"},
    {"name": "beopmang", "status": "ok", "detail": "client wired; Phase 1 stub"},
    {"name": "korean-law-mcp", "status": "ok", "detail": "64 tools documented; Phase 1 stub"}
  ]
}
```

## POST /search

**Request**
```json
{
  "query": "string (required)",
  "mode": "fast | deep",
  "laws": ["013670"]  // optional law_id filter
}
```

**Response 200**
```json
{
  "verdict": "applies | does_not_apply | ambiguous | null",
  "confidence": 0.87,
  "citations": [
    {
      "law_id": "013670",
      "law_name": "수소경제 육성 및 수소 안전관리에 관한 법률",
      "article": "§44",
      "version": "20251001",
      "excerpt": "... max 200 chars of original text ..."
    }
  ],
  "trajectory_id": "uuid | null",
  "mode": "fast | deep"
}
```

`trajectory_id` is non-null only for `mode=deep`. Used for Counsely Track C audit.

## POST /search/batch

**Request**
```json
{
  "queries": [
    {"query": "...", "mode": "fast"},
    {"query": "...", "mode": "deep"}
  ]
}
```

**Response 200**
```json
{
  "results": [SearchResponse, SearchResponse, ...]
}
```

Phase 1: sequential. Phase 2: parallel with asyncio.gather.

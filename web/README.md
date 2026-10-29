# FP/90 · 기록 · 명예의 전당

리그 14명은 **고정 URL만** 엽니다. Fantrax Refresh는 로컬에서만 합니다.

- 당신: `python3 fp90_server.py` → Refresh → `web/` 스냅샷 커밋/푸시
- 친구: GitHub Pages 링크만 연다 (Refresh 버튼 없음)

실시간은 아닙니다. FA·웨이버·점수는 **당신이 Refresh한 뒤 푸시**해야 반영됩니다.

기록/명전은 **완료된 GW만** 씁니다. 진행 중인 GW는 제외됩니다.

## 1. GitHub Pages 켜기 (한 번)

1. 이 레포를 GitHub에 올린다.
2. **Settings → Pages → Source: GitHub Actions**
3. `web/` 과 `.github/workflows/pages.yml` 을 푸시한다.
4. Actions가 끝나면 주소는 보통:

`https://<github-username>.github.io/FantraX/`

레포 이름이 다르면 마지막 경로만 바뀐다. 이 URL을 카톡/북마크에 고정한다.

## 2. 데이터 갱신 (당신이 할 일)

```bash
python3 fp90_server.py
```

브라우저에서 http://127.0.0.1:8765/ 를 열고 **Refresh**.  
끝나면 아래 파일이 덮어써진다.

- `web/fp90.json` — 선수 FP/90
- `web/records.json` — 기록
- `web/hof.json` — 명예의 전당
- `fantrax_data/current/transactions.json` — 웨이버/드랍/트레이드 원본

서버 없이 스냅샷만 쓰려면:

```bash
python3 fp90_server.py --export
```

거래만 다시 받고 기록을 다시 쓰려면:

```bash
python3 records_hub.py
```

그다음:

```bash
git add web/fp90.json web/records.json web/hof.json
git commit -m "Update league snapshots"
git push
```

1–2분 뒤 Pages가 새 JSON을 서빙한다. 친구는 새로고침하면 된다.

## 3. 친구 화면

공개 페이지는 JSON만 GET 한다. `POST /api/refresh` 가 없다.

데이터가 없거나 JSON이 깨지면 화면에 `데이터 없음` 이 뜬다. 추측하지 않는다.

## 하지 말 것

- 공용 URL에 Fantrax Refresh를 열지 말 것
- 친구 브라우저에 Fantrax 쿠키를 요구하지 말 것
- Fantrax.com 안에 이 테이블을 심지 말 것
- ngrok 무료 주소는 매번 바뀌어서 14명 공유에 쓰지 말 것

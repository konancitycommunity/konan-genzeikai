# イベントページ下書き（滋賀県枠）

**未公開**です。GitHub Pages / 月曜の `Update events` Actions には接続していません。

## 何があるか

| ファイル | 役割 |
|---------|------|
| `drafts/events.html` | ライブ `events.html` の下書きコピー。`noindex`・下書きバナー・**滋賀県**枠あり |
| `drafts/events.json` | ライブと同型＋ `"prefecture": [ ... ]` |
| `drafts/update_events_pref.py` | 下書き専用アップデータ（ライブファイルは触らない） |

## 出典

- 滋賀県「催し／イベント情報」  
  https://www.pref.shiga.lg.jp/kensei/koho/e-shinbun/event/index.html  
  （プレスリリース一覧。市民向けのセミナー・フェア・講座などを優先し、審議会・表敬などは控えめ）
- 市主催は従来どおり湖南市 `event_search.html`（ライブ `scripts/update_events.py` のロジックを import）

## 実行方法

リポジトリルート、またはどこからでも:

```bash
pip install requests beautifulsoup4   # 未導入の場合
python3 drafts/update_events_pref.py
```

書き込み先は **`drafts/events.html` と `drafts/events.json` のみ**です。  
`events.html` / `data/events.json` / `scripts/update_events.py` / `.github/workflows` は変更しません。

## 関連（交通・税）への自動振り分け

`drafts/update_events_pref.py` は、市・県から取得した一覧のうち、次のようなキーワードに当てはまる催しを **関連（交通・税）** へ移します（市主催／滋賀県には残しません）。

- 例: `税` / `交通税` / `地域交通` / `討論` / `討論会` / `タウンミーティング` / `県民対話` / `みらいトーク` / `住民説明` / `パブリックコメント`（および説明会）
- 一般の講座・セミナー・スポーツなどは対象外（キーワードは意図的に狭め）
- 手作業の curated（例: SHIGAみらいトーク）を先に載せ、URL・タイトルの重複は除外
- 自動追加はおおむね最大8件（`MAX_RELATED_AUTO`）

ライブの `scripts/update_events.py` / `events.html` / `data/events.json` / Actions はこの下書き専用ロジックを使いません。

## 公開するとき（ユーザーが「公開」と言った場合）

1. `drafts/events.html` の滋賀県セクション（`<!-- AUTO:prefecture -->`）をライブ `events.html` にマージ
2. `scripts/update_events.py` に県スクレイプ・`render_prefecture_html`・JSON の `prefecture` キーを取り込む
3. 月曜 Actions のワークフローはスクリプトパスが同じならそのまま動く想定（必要なら確認）
4. `noindex`・下書きバナー・drafts 専用文言は本番には載せない

それまではナビからライブページへリンクしないまま運用してください。

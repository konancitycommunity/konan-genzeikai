# 湖南市減税会 公式サイト（静的）

モバイルファーストの静的サイトです。ビルド不要で、HTML / CSS / 最小限の JavaScript のみで動作します。

## ローカルでの開き方

### 方法1：ファイルを直接開く

ブラウザで `index.html` を開いてください。

> 一部のブラウザでは、`file://` で開いたときにスクリプトの挙動が制限される場合があります。そのときは方法2を使ってください。

### 方法2：簡易ローカルサーバー（推奨）

サイトのルートで次を実行します。

```bash
cd /path/to/konan-tax-reduction-site
python3 -m http.server 8080
```

ブラウザで [http://localhost:8080/](http://localhost:8080/) を開きます。

（Python 2 の場合は `python -m SimpleHTTPServer 8080` でも可）

## SNS・メール・フォームの編集

`js/config.js` を編集するだけで、全ページの SNS リンク・問い合わせメール・参加フォームが変わります。

```js
window.SITE_CONFIG = {
  mailto: "mailto:konancity20250314@gmail.com",
  googleFormUrl: "", // 設定すると入会ページのフォームボタンが有効化
  sns: {
    x: "https://x.com/nuitshiki",
    youtube: "",       // "" = 今後公開（表示はするがリンクなし）
    note: null,        // null / 省略 = 非表示
    instagram: null
  }
};
```

- `mailto` … 入会ページのメールボタン（`data-mailto`）
- `googleFormUrl` … 入会ページの Googleフォームボタン（`data-google-form`）。空のときは「フォーム準備中」を表示
- `sns` 各キー … SNS ページ・フッター（`data-sns="x"` など）
  - URL 文字列 = 公開リンク
  - `""` = 「今後公開」（カードは表示、フッターは薄く表示）
  - `null` または省略 = 非表示

## ページ構成

| ファイル | 内容 |
|----------|------|
| `index.html` | トップ（団体紹介・3理念・CTA） |
| `reports.html` | 活動報告 |
| `sns.html` | SNS 導線 |
| `events.html` | イベント告知・市政メモ |
| `join.html` | 入会・参加案内 |
| `css/styles.css` | 共通スタイル |
| `js/config.js` | SNS / mailto / フォーム設定 |
| `js/main.js` | ナビ・設定の適用 |
| `favicon.svg` | ファビコン |

## 無料ホスティング（後から）

静的ファイルのみなので、次のような無料ホスティングに載せられます。

- **GitHub Pages** … リポジトリにプッシュし、Pages を有効化
- **Cloudflare Pages** … Git 連携またはフォルダをアップロード

いまは Vercel のセットアップは不要です。公開時に上記のいずれかを選んでください。

## ライセンス・表記

&copy; 2026 湖南市減税会

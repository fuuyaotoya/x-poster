# x-poster — @ThemeMISELL の予約投稿（Mac がオフでも出す）

公開 repo なので GitHub Actions は無料（支払い設定不要）。投稿の中身（本文・画像・予定表）は
もともと公開するものだけを置く。**鍵は Secrets のみ。コードに書かない。**

## 仕組み

- `derivatives/x/queue.tsv` の承認済み行を、JST 12:11 / 12:41 / 22:11（UTC 03:11 / 03:41 / 13:11）に
  `growth/scripts/x_post_due.py --live` が X 公式 API で投稿し、`post_id` を commit して書き戻す
- repo 変数 `X_POST_LIVE` が `true` のときだけ予約投稿が実行される（kill switch）
- 6時間を超えて遅れた行は投稿せず `missed`。新しい `post_at` を承認者が入れる
- 詳しい規則・測定は sugoi-content の misell-x スキルが正本

## セットアップ（1回だけ・オーナー作業）

    gh secret set X_API_KEY      --body "$(python3 -c "import json;print(json.load(open('$HOME/.config/sugoi-growth/x-oauth1.json'))['api_key'])")"
    gh secret set X_API_SECRET   --body "$(python3 -c "import json;print(json.load(open('$HOME/.config/sugoi-growth/x-oauth1.json'))['api_secret'])")"
    gh secret set X_ACCESS_TOKEN --body "$(python3 -c "import json;print(json.load(open('$HOME/.config/sugoi-growth/x-oauth1.json'))['access_token'])")"
    gh secret set X_ACCESS_SECRET --body "$(python3 -c "import json;print(json.load(open('$HOME/.config/sugoi-growth/x-oauth1.json'))['access_secret'])")"
    gh variable set X_POST_LIVE --body true   # 止めるときは false

## sugoi-content からの引っ越し

1. sugoi-content の `derivatives/x/` に新しい投稿を作る（いままでどおり）
2. この repo へ `derivatives/x/queue.tsv`（該当行）・`derivatives/x/posts/*`・`assets/x/*` をコピーして push
3. sugoi-content 側の queue.tsv からその行を消す（二重投稿の防止）
4. Mac の launchd（com.sugoi-content.x-post）は、初回のクラウド投稿を確認してから外す

## 注意

- 鍵（`~/.config/sugoi-growth/x-oauth1.json` の中身）をこの repo のファイルに書かない。Secrets だけ
- この repo は公開。投稿前の本文・画像を第三者が読める（watch している人のみ）。秘密は置かない

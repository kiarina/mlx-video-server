# mlx-video-server

Apple Silicon 上で **動画生成** を行う単機能 HTTP サーバ。
[mlx-video](https://github.com/Blaizzy/mlx-video) の LTX-2 **distilled** パイプラインをラップする。

1 系統のエンドポイントで **text / image / audio** を受け取り、動画を生成して返す。
プロセス内の単一ワーカーが生成を **常に1本ずつ** 実行し（MLX はメモリを大量に使い、かつスレッドアフィニティがある）、
その間も API は状態確認・ファイル取得に応答し続ける。

English: [README.md](README.md)

## 特徴

- **LTX-2 distilled**（高速: 512×512 で 1本あたり約40秒〜1.5分）。
  モードは添付した入力から自動判定: T2V / I2V / I2V(first+last) / A2V / A2V+I2V / T2V+Audio。
- **単一フライトのキュー** — 並行リクエストは受け付けるが生成は重複しない。モデルは一度ロードして常駐。
- **同期・非同期** の両エンドポイントが1つのキューを共有。
- **Files API** — ダウンロード / 一覧 / 削除。生成物はジョブより長生き
  （ジョブは on-memory で再起動で消えるが、ファイルはディスクに残り自己記述的）。

## 必要環境

- Apple Silicon（MLX）
- `ffmpeg`（`PATH` 上。mlx-video がエンコード/ミックスに使用）
- distilled の重みは初回利用時に `prince-canuma/LTX-2-distilled`（約107GB）から DL され、HF にキャッシュされる。

## 起動

```sh
uv run mlx-video-server
# または
uv run python -m mlx_video_server
```

起動時にモデルをロードし、小さなウォームアップ生成（MLXカーネルのコンパイル）を行うため、最初の実リクエストが速い。
`GET /health` の `warm` で確認できる。

## 設定（環境変数）

| 変数 | 既定 | 意味 |
|---|---|---|
| `MLX_VIDEO_MODEL_REPO` | `prince-canuma/LTX-2-distilled` | モデルrepo（distilled の MLX repo であること）|
| `MLX_VIDEO_TEXT_ENCODER_REPO` | (未設定) | テキストエンコーダ repo の上書き |
| `MLX_VIDEO_FILES_ROOT` | `~/.mlx-video-server/files` | 生成物の保存先 |
| `MLX_VIDEO_HOST` / `MLX_VIDEO_PORT` | `127.0.0.1` / `8800` | バインドアドレス |
| `MLX_VIDEO_AUTH_TOKEN` | (未設定) | 設定時、全 `/v1/*` で `Authorization: Bearer <token>` を要求 |
| `MLX_VIDEO_WARMUP` | `1` | 起動時にカーネルをウォームアップ |
| `MLX_VIDEO_MAX_NUM_FRAMES` | `161` | これより大きい要求は拒否（≈6.7秒 @ 24fps）|
| `MLX_VIDEO_MAX_WIDTH` / `MLX_VIDEO_MAX_HEIGHT` | `768` / `768` | 解像度の上限 |

## API

### 生成（同期）— mp4 を返す

```
POST /v1/generate   (multipart/form-data)
```

パート: `params`（JSON文字列・必須）、任意で `image` / `end_image` / `audio` ファイル。
`video/mp4` と `X-File-Id` ヘッダで応答。生成物は永続化され、後から files API で再取得できる。

`params`（distilled で効くノブのみ）:

```jsonc
{
  "prompt": "...",            // 必須
  "width": 512,               // 64の倍数・MAX_WIDTH以下
  "height": 512,              // 64の倍数・MAX_HEIGHT以下
  "num_frames": 97,           // 1 + 8*k・MAX_NUM_FRAMES以下
  "fps": 24,
  "seed": null,               // null でランダム（採用seedは記録される）
  "image_strength": 1.0,      // 0.0–1.0（I2V）。低いほど入力フレームへの拘束が緩む
  "end_image_strength": null, // 既定は image_strength
  "generate_audio": false     // 音声生成（audio ファイルとは排他）
}
```

例:

```sh
curl -s -o out.mp4 -D - http://127.0.0.1:8800/v1/generate \
  -F 'params={"prompt":"Two dogs wearing sunglasses, cinematic, sunset","num_frames":49}' \
  -F 'image=@first_frame.png'
```

音声付き（`generate_audio:true`）— 音声は mp4 に統合される:

```sh
curl -s -o out.mp4 http://127.0.0.1:8800/v1/generate \
  -F 'params={"prompt":"a jazz band playing on stage, upbeat music","num_frames":49,"generate_audio":true}'

ffprobe -v error -show_entries stream=codec_type,codec_name -of csv=p=0 out.mp4
# h264,video
# aac,audio
```

### 生成（非同期）

```
POST   /v1/jobs           -> 202 {job_id, status, queue_position, mode}
GET    /v1/jobs/{job_id}  -> {status, queue_position?, file_id?, error?, timings?}
GET    /v1/jobs           -> 直近・進行中のジョブ
DELETE /v1/jobs/{job_id}  -> queued のジョブをキャンセル（running は 409）
```

`completed` になったら `file_id` を files API で使う。

### Files

```
GET    /v1/files                    -> [{file_id, mode, prompt, params, has_audio, video_bytes, created_at, timings}]
GET    /v1/files/{file_id}          -> 生成物のメタデータ
GET    /v1/files/{file_id}/download -> mp4 ダウンロード（音声生成時は動画に統合済み）
DELETE /v1/files/{file_id}          -> 生成物ディレクトリを削除
```

### ヘルス

```
GET /health -> {status, warm, queue_len, model_repo}
```

### ヘルプ（LLMエージェント向け）

```
GET /help -> 機械可読の使い方ガイド（認証不要）
```

自己記述的な JSON。エンドポイント、パラメータ（実行中の上限込み）、入力→モードの対応、
バリデーション規則、ベンチで得たノウハウを返す（例: distilled はネガティブガイダンスなし／
I2V で動作を起こすには `image_strength` を下げる／"looking at the camera" はズームを誘発するので
"looking ahead" を使う／構図は `seed` を振る）。エージェントはリクエスト組み立て前に `/help` を見るとよい。

## バリデーション

不正な要求はモデルに届く前に `422` で拒否: `width`/`height` は64の倍数かつ上限以内、
`num_frames` は `1 + 8*k` かつ上限以内、`audio` ファイル（A2V）と `generate_audio` は併用不可。

## 保存レイアウト

```
${files_root}/{file_id}/
  ├── video.mp4          # 音声生成時は動画に統合済み
  ├── input_image.*      # 入力の保存（再現性）
  ├── input_end_image.*
  ├── input_audio.*
  └── metadata.json      # prompt, params（採用seed込み）, mode, timings
```

## メモ / ロードマップ

- v1 はリクエストごとに mlx-video の `generate_video()` を呼ぶ。重みは OS/HF キャッシュと MLX カーネルキャッシュに残るため、
  毎回のオーバーヘッドはロード（数秒）のみで、初回コンパイルではない。
  将来的にはロード済みパイプラインを常駐させ、このロードコストも消す最適化が可能。

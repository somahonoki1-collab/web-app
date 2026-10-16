# 0円で公開するための手順

2026年10月15日作成。PythonAnywhereの無料Beginnerプランで、このFlask＋SQLiteアプリを公開するための資料です。アカウント作成・公開先への配置・公開環境での動作確認はまだ行っていません。

## 決めた条件

- 費用は0円。有料プラン、独自ドメイン、自動従量課金は申し込みません。
- URLを知っていれば誰でもアカウントを登録できます。個人の記録はログイン後に利用します。
- 検索結果への掲載は`noindex`で抑えます。URLの秘密保持やアクセス制限を保証する仕組みではありません。
- グループ参加用の招待コードは既存の成長対戦の機能として残します。
- 週1回と大きな更新前に、公開中の記録をPCへバックアップします。
- 公開後はクラウドの記録を正とし、ローカルの古いデータを再アップロードしません。
- パスワードを忘れた場合は管理者が本人確認し、管理画面のコンソールから再設定します。

確認時点の無料枠は、Webアプリ1つ・ワーカー1つ・保存容量512MiB・公開期限1カ月です。月1回程度、Web管理画面で期限を延長します。新規無料アカウントでは定期実行タスクは利用できないため、バックアップや期限延長の自動化は含めていません。制限や提供条件が変わった場合は課金せず、無料で続ける方法を見直します。[無料枠の公式説明](https://help.pythonanywhere.com/pages/FreeAccountsFeatures/)

## 今回用意したもの

- `wsgi.py`：公開用の入口。開発用サーバーを起動せず、デバッグを使いません。
- `runtime_config.py`：公開用の秘密鍵・ホスト名・データ保存先の読み込み。設定や移行するDBが不足したら起動を止めます。
- `time_utils.py`：記録・カレンダー・回復・成長対戦の日付を日本時間に統一します。既存の日本時間の記録は変換しません。
- `manage.py`：公開設定の作成、SQLiteのバックアップ、本人確認後のパスワード再設定。
- `backup_utils.py`：アプリの読み込みやDB移行をせず、稼働中のSQLiteから整合性のあるコピーを作成します。
- `scripts/build_release.py`：公開用ZIPを作成。記録・秘密鍵・バックアップ・調査資料を含めません。
- ログイン保護：Secure/HttpOnly/SameSiteのCookie、7日間のログイン期限、CSRF、送信サイズ制限、公開ホスト名の制限、基本的な応答ヘッダー。
- 試行回数制限：同じ接続元はログイン30回/15分、接続元とユーザー名の組合せは10回/15分、登録は10回/1時間。成功した送信も数えます。超過時は429と待ち時間を返します。接続元はWebサーバーから渡される`REMOTE_ADDR`を使います。公開時に実際の接続元の扱いも確認します。同じ回線の利用者は回数を共有する場合があります。
- パスワード：登録・再設定は8〜128文字。再設定すると、そのユーザーの既存ログインを無効化します。

## 1．無料アカウントを作成する（ユーザーの操作）

1. [無料Beginnerアカウントの登録画面](https://www.pythonanywhere.com/registration/register/beginner/)を開きます。
2. ユーザー名・メールアドレス・パスワードを入力し、利用規約などを確認して登録します。ユーザー名は公開URLにも使われるので、公開してよい名前を選びます。
3. 届いた案内に従ってメール認証を済ませます。
4. 作成できたら、Codexには**ユーザー名だけ**を伝えます。パスワード、認証コード、秘密鍵はチャットに貼りません。

この資料は`www.pythonanywhere.com`で作成した無料アカウントを想定しています。公開URLは`https://ユーザー名.pythonanywhere.com/`です。

## 2．最初に送るファイルを用意する

PCでは次の2種類を用意します。

1. 公開用コード：`dist/liftlog-release.zip`
2. 最新の記録：`manage.py backup`で作成したDBのコピー

公開用ZIPは次で作り直せます。

```powershell
python scripts/build_release.py
python manage.py backup
```

`python`はこのPCでFlaskを実行できるPythonを選びます。バックアップの保存先は実行結果に表示されます。生の`training.db`を通常のファイルコピーで取得する代わりに、バックアップコマンドでコピーを用意します。

**記録の移行は初回の1回だけです。** 最終バックアップを作成したら、公開後の移行確認が終わるまでローカルへの記録追加を止めます。公開開始後は共有URLで記録します。両方へ記録すると後から自動では統合できません。

最初の自分のアプリアカウントは、ローカルで作成済みのDBを使います。空のDBで公開すると先に来た人が最初の利用者になるため、公開用では既存ユーザーがいないDBの起動を止めるようにしました。

## 3．PythonAnywhereにコードを配置する

以下はPythonAnywhereの**Bashコンソール**で行うLinuxの操作です。PCのPowerShellでは実行しません。

1. Files画面で、自分のホームディレクトリに`liftlog-release.zip`をアップロードします。
2. Consoles画面でBashを開き、次を順に実行します。

```bash
mkdir -p ~/web-app
unzip -n ~/liftlog-release.zip -d ~/web-app
mkvirtualenv --python=/usr/bin/python3.13 liftlog
cd ~/web-app
pip install -r requirements.txt
```

このアプリはPython3.13を想定します。Web画面で選ぶPythonも同じバージョンに揃えます。以後、別のコンソールで作業する場合は`workon liftlog`でこの環境を選びます。[公式Flask設定手順](https://help.pythonanywhere.com/pages/Flask/)

`requirements.txt`のGunicornはPythonAnywhereの起動には使いません。Web画面のWSGI経由で起動します。

## 4．秘密鍵とデータをコードの外に配置する

`YOURNAME`は作成したPythonAnywhereのユーザー名に置き換えます。

```bash
cd ~/web-app
python manage.py init-config --host YOURNAME.pythonanywhere.com
```

初回のみ、次を作成します。

```text
/home/YOURNAME/
  web-app/                      アプリのコード（更新する場所）
  .config/liftlog/settings.json  公開用の秘密鍵と設定（外に公開しない）
  liftlog-data/
    training.db                 公開中の記録（初回に配置する）
    backups/                    バックアップ
```

秘密鍵は自動生成し、画面やチャットに表示しません。設定ファイルの権限は作成時に所有者のみ読み書きできる形にします。再度`init-config`を実行しても既存設定を上書きしません。

PCで作成した**最新のバックアップDB**をFiles画面で`/home/YOURNAME/liftlog-data/`へアップロードし、初回に限って`training.db`という名前にします。既にその場所に`training.db`がある場合は作業を止め、既存の公開データか確認します。上書きしません。

ローカルの`instance/secret_key`は送りません。既存アカウントと記録・メニュー・成長対戦はDBに含まれ、公開先では新しい秘密鍵を使うので全員が改めてログインします。

Files画面で確認するだけにし、秘密鍵のファイルをブラウザーの公開パスに置きません。Static filesの設定で、ホームや`liftlog-data`、`.config`、コードのルート全体を公開してはいけません。

## 5．Webアプリを作成する

1. Web画面 → Add a new web app → 無料の`YOURNAME.pythonanywhere.com` → **Manual configuration** → Python3.13を選びます。
2. Virtualenvに`/home/YOURNAME/.virtualenvs/liftlog`を指定します。
3. Source code / Working directoryを設定する欄がある場合は`/home/YOURNAME/web-app`を指定します。
4. Web画面にリンクされたWSGI設定ファイルを開き、内容を`deploy/pythonanywhere_wsgi.py`の内容に置き換えて保存します。
5. Securityの**Force HTTPSを有効**にします。無料の標準ドメインにはHTTPS証明書が用意されています。[HTTPSの公式説明](https://help.pythonanywhere.com/pages/HTTPSSetup/)
6. Reloadを実行します。設定・DBが不足している場合は起動を止めるので、Web画面のError logで不足項目を確認します。デバッグ画面は公開しません。

WSGI設定の内容：

```python
import sys
from pathlib import Path

project = str(Path.home() / "web-app")
if project not in sys.path:
    sys.path.insert(0, project)

from wsgi import application
```

`python app.py`でサーバーを常時起動する運用にはしません。アプリ内の時刻は日本時間なので、サーバーの標準時刻に合わせて既存記録の日付を変更する必要はありません。

## 6．公開後の確認（これから実施する作業）

実装のテストは今回の作業では実行していません。ユーザーから動作確認の依頼を受けたうえで、まず分離したデータで機能を確認し、公開後は次を確認します。

- PCとスマホからHTTPSで開けること、HTTPで開いたらHTTPSへ移動すること。
- 既存ユーザーでログインでき、本人の記録・メニュー・成長対戦が引き継がれていること。
- 新規登録でき、別ユーザーの非公開記録を閲覧・変更できないこと。
- トレーニングの記録・再開・完了・削除、予定、回復、成長対戦の主要操作。
- 日本時間の日付、スマホ幅の画面、ログイン制限、バックアップと復旧、再設定後のログイン無効化。
- パスワード再設定は確認用アカウントを用い、実際の利用者のパスワードを勝手に変更しないこと。

機能確認が終わってから身内にURLを共有します。`noindex`は検索結果の掲載を抑える指示で、検索サービスすべてに強制できるものではありません。[Googleのnoindex説明](https://developers.google.com/search/docs/crawling-indexing/block-indexing)

## 定期運用：バックアップと期限延長

週1回と大きな更新前に、Bashコンソールで実行します。

```bash
workon liftlog
cd ~/web-app
python manage.py --production backup
```

表示された`liftlog-data/backups/training-日時.db`をFiles画面からPCへダウンロードします。これはアカウントや個人のトレーニング記録を含むので、共有URLやGitHubへ載せません。設定ファイルも管理者だけが扱う場所へ別途保管します。アプリのバージョンとバックアップ日時を一緒に残します。

月1回程度、Web画面で公開期限を確認して延長します。無料容量には仮想環境・コード・DB・バックアップ・ログも含まれるので、不要になったアップロードZIPや、PCへの保存を確認した古いサーバーバックアップは管理者が整理します。自動削除は行いません。

## パスワードを忘れた人がいたら

管理者が、既知の連絡手段で本人から依頼されたことと対象のアプリユーザー名を確認してから、Bashコンソールで実行します。

```bash
workon liftlog
cd ~/web-app
python manage.py --production reset-password APP_USERNAME --verified
```

`APP_USERNAME`はPythonAnywhereのユーザー名ではなく、この筋トレアプリのユーザー名です。新しいパスワードと確認用パスワードを非表示の入力欄で入力します。コマンド引数・チャット・スクリーンショットにパスワードを残しません。新しいパスワードは本人に安全な手段で伝えます。

変更前に自動でバックアップを作成します。変更時は既存ログインを無効化し、次の操作で再ログインを求めます。`--verified`は本人確認済みの明示であり、本人確認を自動で行う機能ではありません。このコマンドを実行できるのはホスティングの管理者だけです。

## 今後の更新

1. 利用者に更新を案内し、公開先の最新DBをバックアップしてPCへ保存します。
2. PCでコードを改良し、依頼された動作確認を行い、公開用ZIPを作り直します。
3. Filesで新しい`liftlog-release.zip`をホームへ送り、Bashでコードだけを更新します。

```bash
workon liftlog
cd ~/web-app
unzip -o ~/liftlog-release.zip -d ~/web-app
```

4. Web画面でReloadします。既存DBへの追加列・新規テーブルは起動時の移行で反映します。将来、列削除など大きな変更をする場合は個別に移行手順を作ります。
5. 公開先で動作を確認します。記録DBと秘密鍵はコードの外にあるので、通常のコード更新で置き換わりません。

**更新時は、DBをローカルからアップロードし直しません。** 更新で不具合が起きた場合は、エラーログとバックアップを残し、コードとDBの形式を合わせて復旧します。古いDBをそのまま上書きするとバックアップ後の新しい記録を失うため、復旧は利用を止めて別途手順を決めます。

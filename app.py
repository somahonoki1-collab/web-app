from flask import Flask, render_template, request, redirect, url_for
from flask_sqlalchemy import SQLAlchemy # データベース用の道具
from datetime import datetime # 日付・時刻を扱うための道具

app = Flask(__name__)

# --- 1. データベースの基本設定 ---
# SQLiteを使い、training.dbというファイルにデータを保存する設定
app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///training.db'
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
db = SQLAlchemy(app)

# --- 2. テーブル（表）の設計図：Javaのクラスと同じ感覚 ---
class TrainingLog(db.Model):
    id = db.Column(db.Integer, primary_key=True) # 自動で振られる番号
    date = db.Column(db.DateTime, nullable=False, default=datetime.now) # 記録した日時
    lift_type = db.Column(db.String(50), nullable=False) # 種目
    weight = db.Column(db.Float, nullable=False) # 扱った重量
    reps = db.Column(db.Integer, nullable=False) # 回数
    one_rm = db.Column(db.Float, nullable=False) # 計算された1RM
    target_reps = db.Column(db.Integer, nullable=False) # 目標回数
    target_weight = db.Column(db.Float, nullable=False) # 提案された重量

# 初回起動時にデータベースファイル（training.db）を自動で作成する
with app.app_context():
    db.create_all()

# --- 今までの計算関数 ---
def calculate_1rm(weight, reps, lift_type):
    if lift_type == "ベンチプレス":
        return weight * (1 + reps / 40.0)
    elif lift_type == "スクワット":
        return weight * (1 + reps / 33.3)
    else:
        return weight * (1 + reps / 40.0)

def calculate_target_weight(max_weight, target_reps, lift_type):
    if lift_type == "ベンチプレス":
        return max_weight / (1 + target_reps / 40.0)
    elif lift_type == "スクワット":
        return max_weight / (1 + target_reps / 33.3)
    else:
        return max_weight / (1 + target_reps / 40.0)

@app.route("/", methods=["GET", "POST"])
def home():
    max_weight = None
    next_weight = None

    if request.method == "POST":
        lift = request.form.get("lift_type")
        last_weight = float(request.form.get("weight"))
        last_reps = int(request.form.get("reps"))
        target = int(request.form.get("target_reps"))
        
        max_weight = calculate_1rm(last_weight, last_reps, lift)
        next_weight = calculate_target_weight(max_weight, target, lift)
        max_weight = round(max_weight, 1)
        next_weight = round(next_weight, 1)

        # --- 3. データベースに保存 ---
        # 1回分のトレーニング結果をインスタンス化
        new_log = TrainingLog(
            lift_type=lift,
            weight=last_weight,
            reps=last_reps,
            one_rm=max_weight,
            target_reps=target,
            target_weight=next_weight
        )
        db.session.add(new_log) # DBに追加
        db.session.commit()     # 確定して保存

    # --- 4. データベースから読み込み ---
    # 保存されている記録を、日付の新しい順（desc）に全て（all）取得する
    all_logs = TrainingLog.query.order_by(TrainingLog.date.desc()).all()

    # 取得した all_logs をHTMLに渡す
    return render_template("index.html", max_w=max_weight, target_w=next_weight, logs=all_logs)

# --- 5. データベースから削除 ---
@app.route("/delete/<int:log_id>", methods=["POST"])
def delete_log(log_id):
    # 送られてきたIDと一致する記録をデータベースから探し出す
    log_to_delete = TrainingLog.query.get(log_id)
    
    if log_to_delete:
        db.session.delete(log_to_delete) # 削除の予約
        db.session.commit()              # 確定して保存
        
    # 削除が終わったら、home関数に自動で戻る
    return redirect(url_for('home'))

if __name__ == "__main__":
    app.run(debug=True)
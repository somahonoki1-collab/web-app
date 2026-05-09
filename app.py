from flask import Flask, render_template, request

app = Flask(__name__)

# 一緒に作った種目別の1RM計算関数
def calculate_1rm(weight, reps, lift_type):
    if lift_type == "ベンチプレス":
        return weight * (1 + reps / 40.0)
    elif lift_type == "スクワット":
        return weight * (1 + reps / 33.3)
    else:
        return weight * (1 + reps / 40.0)

# 自力で書いた目標重量の逆算関数！（変数名とカンマだけ修正しました）
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
    next_weight = None # エラーを防ぐためにこれも初期化

    if request.method == "POST":
        # 画面から4つのデータを受け取る
        lift = request.form.get("lift_type")
        last_weight = float(request.form.get("weight"))
        last_reps = int(request.form.get("reps"))
        target = int(request.form.get("target_reps")) # 新しく追加した目標回数
        
        # 1RMを計算
        max_weight = calculate_1rm(last_weight, last_reps, lift)
        
        # 1RMをもとに、目標回数での重量を逆算！
        next_weight = calculate_target_weight(max_weight, target, lift)

        # 小数点第1位で丸める
        max_weight = round(max_weight, 1)
        next_weight = round(next_weight, 1)

    # next_weightをtarget_wという名前でHTMLに渡す
    return render_template("index.html", max_w=max_weight, target_w=next_weight)

if __name__ == "__main__":
    app.run(debug=True)
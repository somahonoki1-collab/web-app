from flask import Flask

app = Flask(__name__)

# トップページ（/）にアクセスした時の動作
@app.route("/")
def hello():
    return "<h1>Hello, Web Development!</h1><p>開発手順を覚えた！</p>"

if __name__ == "__main__":
    # debug=True にすると、コードを書き換えた時に自動で反映されます
    app.run(debug=True)
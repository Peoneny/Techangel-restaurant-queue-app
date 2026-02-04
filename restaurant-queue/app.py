from flask import Flask, request, jsonify, render_template
from threading import Lock

app = Flask(__name__)

queue = []
lock = Lock()

@app.route('/')
def home():
    return render_template('index.html')

@app.route('/reserve', methods=['POST'])
def reserve():
    name = request.json['name']
    with lock:
        queue.append(name)
        position = len(queue)
    return jsonify({"queue": position})

@app.route('/queue', methods=['GET'])
def show_queue():
    return jsonify(queue)

@app.route('/next', methods=['POST'])
def next_queue():
    with lock:
        if queue:
            return jsonify({"called": queue.pop(0)})
        return jsonify({"called": None})

app.run(host="0.0.0.0", port=8000)

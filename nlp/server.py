"""
The news desk's sentiment scorer: FinBERT (ProsusAI's BERT fine-tuned on
financial news and reports), the 8-bit ONNX export (Xenova/finbert,
model_quantized.onnx), run with onnxruntime on the CPU.

One endpoint, reachable only from inside the compose network:

    POST /score  {"texts": ["Tata Chemicals Q2 profit beats estimates", ...]}
    ->  {"model": "finbert-int8", "results": [{"label": "positive", "score": 0.91,
          "probs": {"positive": 0.93, "negative": 0.02, "neutral": 0.05}}, ...]}
    GET  /health

`score` is P(positive) - P(negative), from -1 (clearly negative) to +1. A
headline is read as written: FinBERT does not know what the market expected,
so "profit falls 5%" is negative even when the street feared worse.
"""
import json
import logging
import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import numpy as np
import onnxruntime as ort
from tokenizers import Tokenizer

MODEL_DIR = os.getenv('MODEL_DIR', '/model')
MAX_TOKENS = 128            # headlines and filing summaries are short; longer text is cut
MAX_TEXTS = 64              # per request
MODEL_NAME = 'finbert-int8'

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(message)s')
log = logging.getLogger('nlp')

with open(os.path.join(MODEL_DIR, 'config.json')) as f:
    LABELS = {int(k): v for k, v in json.load(f)['id2label'].items()}
TOK = Tokenizer.from_file(os.path.join(MODEL_DIR, 'tokenizer.json'))
TOK.enable_truncation(MAX_TOKENS)
TOK.enable_padding(pad_id=0, pad_token='[PAD]')
opts = ort.SessionOptions()
opts.intra_op_num_threads = int(os.getenv('THREADS', '1'))     # one core: the scanner comes first
opts.inter_op_num_threads = 1
SESSION = ort.InferenceSession(os.path.join(MODEL_DIR, 'model_quantized.onnx'), opts,
                               providers=['CPUExecutionProvider'])
INPUTS = {i.name for i in SESSION.get_inputs()}
LOCK = threading.Lock()     # onnxruntime is thread-safe, but one inference at a time keeps it to one core


def score(texts):
    texts = [str(t or '')[:1000] or '.' for t in texts]
    enc = TOK.encode_batch(texts)
    feed = {'input_ids': np.array([e.ids for e in enc], dtype=np.int64),
            'attention_mask': np.array([e.attention_mask for e in enc], dtype=np.int64)}
    if 'token_type_ids' in INPUTS:
        feed['token_type_ids'] = np.array([e.type_ids for e in enc], dtype=np.int64)
    with LOCK:
        logits = SESSION.run(None, feed)[0]
    e = np.exp(logits - logits.max(axis=1, keepdims=True))
    probs = e / e.sum(axis=1, keepdims=True)
    out = []
    for p in probs:
        named = {LABELS[k]: round(float(v), 4) for k, v in enumerate(p)}
        out.append({'label': max(named, key=named.get), 'probs': named,
                    'score': round(named.get('positive', 0) - named.get('negative', 0), 4)})
    return out


class Handler(BaseHTTPRequestHandler):
    def _send(self, code, body):
        data = json.dumps(body).encode()
        self.send_response(code)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if self.path == '/health':
            return self._send(200, {'ok': True, 'model': MODEL_NAME})
        self._send(404, {'error': 'not found'})

    def do_POST(self):
        if self.path != '/score':
            return self._send(404, {'error': 'not found'})
        try:
            n = int(self.headers.get('Content-Length') or 0)
            if n > 2_000_000:
                return self._send(413, {'error': 'too large'})
            texts = json.loads(self.rfile.read(n) or b'{}').get('texts')
            if not isinstance(texts, list) or not texts or len(texts) > MAX_TEXTS:
                return self._send(400, {'error': f'send {{"texts": [...]}} with 1 to {MAX_TEXTS} strings'})
            self._send(200, {'model': MODEL_NAME, 'results': score(texts)})
        except (ValueError, AttributeError) as e:
            self._send(400, {'error': str(e)[:200]})

    def log_message(self, fmt, *args):      # one line per request is noise; errors still log
        pass


if __name__ == '__main__':
    score(['warm-up'])
    log.info(f'FinBERT ready ({MODEL_NAME}, inputs {sorted(INPUTS)}, labels {LABELS})')
    ThreadingHTTPServer(('0.0.0.0', int(os.getenv('PORT', '8000'))), Handler).serve_forever()

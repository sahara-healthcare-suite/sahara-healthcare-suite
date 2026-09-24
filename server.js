const http = require('http');
const fs = require('fs');
const path = require('path');
const { URL } = require('url');

const PORT = process.env.PORT || 3000;
const ROOT = path.resolve(__dirname);
const CONTENT_TYPES = {
  '.html': 'text/html; charset=utf-8',
  '.js': 'text/javascript; charset=utf-8',
  '.css': 'text/css; charset=utf-8',
  '.json': 'application/json; charset=utf-8',
  '.png': 'image/png',
  '.jpg': 'image/jpeg',
  '.svg': 'image/svg+xml'
};

function sendJson(res, statusCode, payload) {
  res.writeHead(statusCode, {
    'Content-Type': 'application/json; charset=utf-8',
    'Access-Control-Allow-Origin': process.env.ALLOWED_ORIGIN || 'http://localhost:3000',
    'Vary': 'Origin'
  });
  res.end(JSON.stringify(payload));
}

function readJsonBody(req) {
  return new Promise((resolve, reject) => {
    let body = '';
    req.setEncoding('utf8');
    req.on('data', (chunk) => {
      body += chunk;
      if (body.length > 10 * 1024 * 1024) {
        reject(new Error('Request body exceeds 10 MB'));
        req.destroy();
      }
    });
    req.on('end', () => {
      try {
        resolve(body ? JSON.parse(body) : {});
      } catch (error) {
        reject(error);
      }
    });
    req.on('error', reject);
  });
}

async function handleIntronTranscription(req, res) {
  const apiKey = process.env.INTRON_API_KEY;
  if (!apiKey) {
    return sendJson(res, 401, {
      error: 'Missing Intron API key. Configure INTRON_API_KEY on the server.'
    });
  }

  try {
    const payload = await readJsonBody(req);
    const authorization = apiKey.startsWith('Bearer ') ? apiKey : `Bearer ${apiKey}`;
    const response = await fetch('https://infer.voice.intron.io/v1/transcribe', {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        Authorization: authorization
      },
      body: JSON.stringify(payload)
    });
    const data = await response.json();
    return sendJson(res, response.status, data);
  } catch (error) {
    console.error('Error proxying request to Intron API:', error);
    return sendJson(res, 502, { error: 'Failed to communicate with Intron Voice API.' });
  }
}

function serveStatic(res, requestUrl) {
  const requestedPath = requestUrl.pathname === '/' ? '/index.html' : requestUrl.pathname;
  const filePath = path.resolve(ROOT, `.${requestedPath}`);
  const relativePath = path.relative(ROOT, filePath);
  if (relativePath.startsWith('..') || path.isAbsolute(relativePath)) {
    return sendJson(res, 403, { error: 'Forbidden' });
  }

  fs.readFile(filePath, (error, content) => {
    if (error) {
      if (error.code === 'ENOENT') {
        return fs.readFile(path.join(ROOT, 'index.html'), (fallbackError, fallback) => {
          if (fallbackError) return sendJson(res, 404, { error: 'Not found' });
          res.writeHead(200, { 'Content-Type': CONTENT_TYPES['.html'] });
          res.end(fallback);
        });
      }
      return sendJson(res, 500, { error: 'Failed to read the requested asset' });
    }
    const extension = path.extname(filePath).toLowerCase();
    res.writeHead(200, { 'Content-Type': CONTENT_TYPES[extension] || 'application/octet-stream' });
    res.end(content);
  });
}

const server = http.createServer(async (req, res) => {
  if (req.method === 'OPTIONS') {
    res.writeHead(204, {
      'Access-Control-Allow-Origin': process.env.ALLOWED_ORIGIN || 'http://localhost:3000',
      'Vary': 'Origin',
      'Access-Control-Allow-Headers': 'Origin, X-Requested-With, Content-Type, Accept',
      'Access-Control-Allow-Methods': 'GET, POST, OPTIONS'
    });
    return res.end();
  }

  const requestUrl = new URL(req.url, `http://${req.headers.host || 'localhost'}`);
  if (req.method === 'POST' && requestUrl.pathname === '/api/intron/transcribe') {
    return handleIntronTranscription(req, res);
  }
  if (req.method === 'GET') {
    return serveStatic(res, requestUrl);
  }
  return sendJson(res, 405, { error: 'Method not allowed' });
});

server.listen(PORT, () => {
  console.log('================================================');
  console.log(`AfriHealth AI Server running on port ${PORT}`);
  console.log('================================================');
});

const DB_NAME = 'sahara-healthcare-suite';
const STORE_NAME = 'offline-sync-queue';

function openDb() {
  return new Promise((resolve, reject) => {
    const request = indexedDB.open(DB_NAME, 1);
    request.onupgradeneeded = () => {
      const db = request.result;
      if (!db.objectStoreNames.contains(STORE_NAME)) {
        db.createObjectStore(STORE_NAME, { keyPath: 'id', autoIncrement: true });
      }
    };
    request.onsuccess = () => resolve(request.result);
    request.onerror = () => reject(request.error);
  });
}

async function readPendingRecords() {
  const db = await openDb();
  return new Promise((resolve, reject) => {
    const transaction = db.transaction(STORE_NAME, 'readonly');
    const store = transaction.objectStore(STORE_NAME);
    const request = store.getAll();
    request.onsuccess = () => resolve(request.result || []);
    request.onerror = () => reject(request.error);
  });
}

self.onmessage = async (event) => {
  if (event.data && event.data.type === 'clear') {
    try {
      const db = await openDb();
      const transaction = db.transaction(STORE_NAME, 'readwrite');
      const request = transaction.objectStore(STORE_NAME).clear();
      request.onsuccess = () => self.postMessage({ type: 'cleared' });
      request.onerror = () => self.postMessage({ type: 'error', error: String(request.error) });
    } catch (error) {
      self.postMessage({ type: 'error', error: String(error) });
    }
  } else if (event.data && event.data.type === 'flush') {
    try {
      const pending = await readPendingRecords();
      self.postMessage({ type: 'pending', records: pending });
    } catch (error) {
      self.postMessage({ type: 'error', error: String(error) });
    }
  }
};

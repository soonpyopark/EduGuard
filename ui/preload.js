'use strict';

const { contextBridge, ipcRenderer } = require('electron');

function subscribe(channel, callback) {
  const listener = (_e, payload) => callback(payload);
  ipcRenderer.on(channel, listener);
  return () => ipcRenderer.removeListener(channel, listener);
}

contextBridge.exposeInMainWorld('eduguard', {
  api: (method, apiPath, body) => ipcRenderer.invoke('api', method, apiPath, body),
  info: () => ipcRenderer.invoke('app:info'),
  exportKeywords: (keywords) => ipcRenderer.invoke('keywords:export', keywords),
  importKeywords: () => ipcRenderer.invoke('keywords:import'),
  openExternal: (url) => ipcRenderer.invoke('open-external', url),
  restartBackend: () => ipcRenderer.invoke('backend:restart'),
  quit: () => ipcRenderer.invoke('app:quit'),
  onPoll: (cb) => subscribe('poll', cb),
  onBackend: (cb) => subscribe('backend', cb),
  onRequestExit: (cb) => subscribe('request-exit', cb),
});

/*! JASSUB 2.5.20 worker (MIT) + abslink 1.3.1 (Apache-2.0). jassub-worker*.wasm is libass (ISC) with FreeType
 (FTL: portions of this software are copyright The FreeType Project, www.freetype.org), FriBidi (LGPL-2.1-or-later,
 source: github.com/fribidi/fribidi), HarfBuzz (MIT, old), Brotli (MIT) and expat (MIT), compiled with Emscripten by
 JASSUB (source and build: github.com/ThaUnknown/jassub, commit dea3d2d398 = npm 2.5.20). */

// ../../../../../../qa/lp-captions/jassub-build/pkgs/abslink/package/src/types.js
var WireValueType = {
  RAW: "RAW",
  PROXY: "PROXY",
  THROW: "THROW",
  HANDLER: "HANDLER"
};
var MessageType = {
  GET: "GET",
  SET: "SET",
  APPLY: "APPLY",
  CONSTRUCT: "CONSTRUCT",
  RELEASE: "RELEASE"
};

// ../../../../../../qa/lp-captions/jassub-build/pkgs/abslink/package/src/abslink.js
var proxyMarker = /* @__PURE__ */ Symbol("Abslink.proxy");
var releaseProxy = /* @__PURE__ */ Symbol("Abslink.releaseProxy");
var finalizer = /* @__PURE__ */ Symbol("Abslink.finalizer");
var throwMarker = /* @__PURE__ */ Symbol("Abslink.thrown");
var isObject = (val) => typeof val === "object" && val !== null || typeof val === "function";
var proxyTransferHandler = {
  canHandle: (val) => isObject(val) && proxyMarker in val,
  serialize(obj, ep) {
    const markerID = obj[proxyMarker];
    expose(obj, ep, markerID);
    return [markerID, []];
  },
  deserialize(markerID, ep) {
    return wrap(ep, markerID);
  }
};
var throwTransferHandler = {
  canHandle: (value) => isObject(value) && throwMarker in value,
  serialize({ value }) {
    let serialized;
    if (value instanceof Error) {
      serialized = {
        isError: true,
        value: {
          message: value.message,
          name: value.name,
          stack: value.stack
        }
      };
    } else {
      serialized = { isError: false, value };
    }
    return [serialized, []];
  },
  deserialize(serialized) {
    if (serialized.isError) {
      throw Object.assign(new Error(serialized.value.message), serialized.value);
    }
    throw serialized.value;
  }
};
var transferHandlers = /* @__PURE__ */ new Map([
  ["proxy", proxyTransferHandler],
  ["throw", throwTransferHandler]
]);
function filterPath(path, obj) {
  let parent = obj;
  const parentPath = path.slice(0, -1);
  for (const segment of parentPath) {
    if (Object.prototype.hasOwnProperty.call(parent, segment)) {
      parent = parent[segment];
    }
  }
  const lastSegment = path[path.length - 1];
  const RawValue = lastSegment ? parent[lastSegment] : parent;
  return { parent, RawValue, lastSegment };
}
function expose(obj, ep, rootMarkerID) {
  ep.on("message", function callback(data) {
    if (!data)
      return;
    const { id, type, path, markerID } = {
      path: [],
      ...data
    };
    if (markerID !== rootMarkerID)
      return;
    const argumentList = (data.argumentList ?? []).map((v) => fromWireValue(v, ep));
    let returnValue;
    try {
      const { parent, RawValue, lastSegment } = filterPath(path, obj);
      switch (type) {
        case MessageType.GET:
          returnValue = RawValue;
          break;
        case MessageType.SET:
          parent[lastSegment] = fromWireValue(data.value, ep);
          returnValue = true;
          break;
        case MessageType.APPLY:
          returnValue = RawValue.apply(parent, argumentList);
          break;
        case MessageType.CONSTRUCT:
          returnValue = new RawValue(...argumentList);
          break;
        case MessageType.RELEASE:
          returnValue = void 0;
          break;
        default:
          return;
      }
    } catch (value) {
      returnValue = { value, [throwMarker]: 0 };
    }
    Promise.resolve(returnValue).catch((value) => {
      return { value, [throwMarker]: 0 };
    }).then((returnValue2) => {
      if (type === MessageType.CONSTRUCT && !(isObject(returnValue2) && throwMarker in returnValue2)) {
        returnValue2 = proxy(returnValue2);
      }
      const [wireValue, transfer] = toWireValue(returnValue2, ep);
      ep.postMessage({ ...wireValue, id, markerID: rootMarkerID }, transfer);
      if (type === MessageType.RELEASE) {
        ep.off("message", callback);
        obj[finalizer]?.();
      }
    }).catch((_) => {
      const [wireValue, transfer] = toWireValue({
        value: new TypeError("Unserializable return value"),
        [throwMarker]: 0
      }, ep);
      ep.postMessage({ ...wireValue, id, markerID: rootMarkerID }, transfer);
    });
  });
  return obj;
}
function wrap(endpoint, rootMarkerID) {
  const pendingListeners = /* @__PURE__ */ new Map();
  endpoint.on("message", (data) => {
    if (!data?.id) {
      return;
    }
    const resolver = pendingListeners.get(data.id);
    if (!resolver) {
      return;
    }
    try {
      resolver(data);
    } finally {
      pendingListeners.delete(data.id);
    }
  });
  return createProxy({ endpoint, pendingListeners, rootMarkerID });
}
function throwIfProxyReleased(isReleased) {
  if (isReleased) {
    throw new Error("Proxy has been released and is not useable");
  }
}
async function releaseEndpoint(epWithPendingListeners) {
  await requestResponseMessage(epWithPendingListeners, { type: MessageType.RELEASE });
}
var proxyCounter = /* @__PURE__ */ new WeakMap();
var proxyFinalizers = "FinalizationRegistry" in globalThis && new FinalizationRegistry((epWithPendingListeners) => {
  const newCount = (proxyCounter.get(epWithPendingListeners) ?? 0) - 1;
  proxyCounter.set(epWithPendingListeners, newCount);
  if (newCount === 0) {
    releaseEndpoint(epWithPendingListeners).finally(() => {
      epWithPendingListeners.pendingListeners.clear();
    });
  }
});
function registerProxy(proxy2, epWithPendingListeners) {
  const newCount = (proxyCounter.get(epWithPendingListeners) ?? 0) + 1;
  proxyCounter.set(epWithPendingListeners, newCount);
  if (proxyFinalizers) {
    proxyFinalizers.register(proxy2, epWithPendingListeners, proxy2);
  }
}
function unregisterProxy(proxy2) {
  if (proxyFinalizers) {
    proxyFinalizers.unregister(proxy2);
  }
}
function createProxy(epWithPendingListeners, path = []) {
  let isProxyReleased = false;
  const propProxyCache = /* @__PURE__ */ new Map();
  const proxy2 = new Proxy(function() {
  }, {
    get(_target, prop) {
      throwIfProxyReleased(isProxyReleased);
      if (prop === releaseProxy) {
        return async () => {
          for (const subProxy of propProxyCache.values()) {
            subProxy[releaseProxy]();
          }
          propProxyCache.clear();
          unregisterProxy(proxy2);
          releaseEndpoint(epWithPendingListeners).finally(() => {
            epWithPendingListeners.pendingListeners.clear();
          });
          isProxyReleased = true;
        };
      }
      if (prop === "then") {
        if (path.length === 0) {
          return { then: () => proxy2 };
        }
        const r = requestResponseMessage(epWithPendingListeners, {
          type: MessageType.GET,
          path: path.map((p) => p.toString())
        }).then((v) => fromWireValue(v, epWithPendingListeners.endpoint));
        return r.then.bind(r);
      }
      const cachedProxy = propProxyCache.get(prop);
      if (cachedProxy) {
        return cachedProxy;
      }
      const propProxy = createProxy(epWithPendingListeners, [...path, prop]);
      propProxyCache.set(prop, propProxy);
      return propProxy;
    },
    set(_target, prop, rawValue) {
      throwIfProxyReleased(isProxyReleased);
      const [value, transfer] = toWireValue(rawValue, epWithPendingListeners.endpoint);
      return requestResponseMessage(epWithPendingListeners, {
        type: MessageType.SET,
        path: [...path, prop].map((p) => p.toString()),
        value
      }, transfer).then((v) => fromWireValue(v, epWithPendingListeners.endpoint));
    },
    apply(_target, _thisArg, rawArgumentList) {
      throwIfProxyReleased(isProxyReleased);
      const last = path[path.length - 1];
      if (last === "bind") {
        return createProxy(epWithPendingListeners, path.slice(0, -1));
      }
      const [argumentList, transfer] = processArguments(rawArgumentList, epWithPendingListeners);
      return requestResponseMessage(epWithPendingListeners, {
        type: MessageType.APPLY,
        path: path.map((p) => p.toString()),
        argumentList
      }, transfer).then((v) => fromWireValue(v, epWithPendingListeners.endpoint));
    },
    construct(_target, rawArgumentList) {
      throwIfProxyReleased(isProxyReleased);
      const [argumentList, transfer] = processArguments(rawArgumentList, epWithPendingListeners);
      return requestResponseMessage(epWithPendingListeners, {
        type: MessageType.CONSTRUCT,
        path: path.map((p) => p.toString()),
        argumentList
      }, transfer).then((v) => fromWireValue(v, epWithPendingListeners.endpoint));
    }
  });
  registerProxy(proxy2, epWithPendingListeners);
  return proxy2;
}
var transferCache = /* @__PURE__ */ new WeakMap();
function processArguments(argumentList, epWithPendingListeners) {
  const wireValues = [];
  const transferables = [];
  for (const argument of argumentList) {
    const [wireValue, transfer] = toWireValue(argument, epWithPendingListeners.endpoint);
    wireValues.push(wireValue);
    transferables.push(...transfer);
  }
  return [wireValues, transferables];
}
function proxy(obj) {
  return Object.assign(obj, { [proxyMarker]: randomId() });
}
function toWireValue(value, ep) {
  for (const [name, handler] of transferHandlers) {
    if (handler.canHandle(value)) {
      const [serializedValue, transfer] = handler.serialize(value, ep);
      return [{
        type: WireValueType.HANDLER,
        name,
        value: serializedValue
      }, transfer];
    }
  }
  return [{
    type: WireValueType.RAW,
    value
  }, transferCache.get(value) ?? []];
}
function fromWireValue(value, ep) {
  switch (value.type) {
    case WireValueType.HANDLER:
      return transferHandlers.get(value.name).deserialize(value.value, ep);
    case WireValueType.RAW:
      return value.value;
  }
}
function requestResponseMessage(ep, msg, transfer = []) {
  return new Promise((resolve) => {
    const id = randomId();
    ep.pendingListeners.set(id, resolve);
    ep.endpoint.postMessage({ id, ...msg, markerID: ep.rootMarkerID }, transfer);
  });
}
var hex = [];
var alphabet = "0123456789abcdef";
for (let i = 0; i < 256; i++) {
  hex[i] = alphabet[i >> 4 & 15] + alphabet[i & 15];
}
var step = 0;
var buffer = "";
function randomId() {
  let i = 0;
  if (!buffer || step + 16 > 256 * 2) {
    for (buffer = "", step = 0; i < 256; ++i) {
      buffer += hex[Math.random() * 256 | 0];
    }
  }
  return buffer.substring(step, ++step + 16);
}

// ../../../../../../qa/lp-captions/jassub-build/pkgs/abslink/package/adapters/w3c.js
function createWrapper(channel, messageable) {
  const listeners = /* @__PURE__ */ new WeakMap();
  channel.start?.();
  messageable.start?.();
  return {
    on(event, listener) {
      const unwrapped = (event2) => listener(event2.data);
      if ("addEventListener" in channel) {
        channel.addEventListener(event, unwrapped);
      } else if ("addListener" in channel) {
        channel.addListener(event, unwrapped);
      } else {
        channel.on(event, unwrapped);
      }
      listeners.set(listener, unwrapped);
    },
    off(event, listener) {
      const unwrapped = listeners.get(listener);
      if ("removeEventListener" in channel) {
        channel.removeEventListener(event, unwrapped);
      } else if ("removeListener" in channel) {
        channel.removeListener(event, unwrapped);
      } else {
        channel.off(event, unwrapped);
      }
      listeners.delete(listener);
    },
    postMessage(message, transfer = []) {
      messageable.postMessage(message, transfer);
    }
  };
}
function expose2(obj, channel = globalThis, messageable = channel) {
  return expose(obj, createWrapper(channel, messageable));
}

// ../../../../../../qa/lp-captions/jassub-build/node_modules/lfa-ponyfill/index.js
async function queryRemoteFonts() {
  return [];
}

// ../../../../../../qa/lp-captions/jassub-build/jassub/dist/wasm/jassub-worker.js
if (self.name.startsWith("em-pthread")) {
  const url = self.name.split("-").slice(2).join("-");
  const _fetch2 = globalThis.fetch;
  globalThis.fetch = (_) => _fetch2(url);
  self.name = "em-pthread";
}
async function Module(moduleArg = {}) {
  var Module2 = moduleArg;
  var ENVIRONMENT_IS_WEB = true;
  var ENVIRONMENT_IS_WORKER = !!globalThis.WorkerGlobalScope;
  var out = (...args) => console.log(...args);
  var err = (...args) => console.error(...args);
  function ready() {
    if (ENVIRONMENT_IS_PTHREAD) {
      startWorker();
    }
  }
  var ENVIRONMENT_IS_PTHREAD = ENVIRONMENT_IS_WORKER && globalThis.name == "em-pthread";
  if (ENVIRONMENT_IS_WORKER) {
    _scriptName = self.location.href;
  }
  var _scriptName;
  updateMemoryViews = /* @__PURE__ */ ((o) => () => {
    o();
    self.HEAPU8RAW = (growMemViews(), HEAPU8);
    self.WASMMEMORY = wasmMemory;
  })(updateMemoryViews);
  out = moduleArg.__out || out;
  if (!self.name.startsWith("em-pthread")) {
    const OriginalWorker = globalThis.Worker;
    globalThis.Worker = class extends OriginalWorker {
      constructor(scriptURL, options = {}) {
        super(scriptURL, { ...options, name: "em-pthread-" + moduleArg.__url });
      }
    };
  }
  const _origLoadModule = loadModule;
  loadModule = function() {
    _origLoadModule();
    ASM_CONSTS = new Proxy({}, { get() {
      return ($0) => stringToNewUTF8(Emval.toValue($0));
    } });
  };
  if (!ENVIRONMENT_IS_PTHREAD) {
  }
  function abort(what) {
    throw what;
  }
  class EmscriptenEH {
  }
  class EmscriptenSjLj extends EmscriptenEH {
  }
  function growMemViews() {
    if (wasmMemory.buffer != HEAP8.buffer) {
      updateMemoryViews();
    }
  }
  var startWorker;
  if (ENVIRONMENT_IS_PTHREAD) {
    let handleMessage = function(e) {
      try {
        var msgData = e.data;
        var cmd = msgData.cmd;
        if (cmd == 1) {
          let messageQueue = [];
          self.onmessage = (e2) => messageQueue.push(e2);
          startWorker = () => {
            postMessage({ cmd: 3 });
            for (let msg of messageQueue) {
              handleMessage(msg);
            }
            self.onmessage = handleMessage;
          };
          for (const handler of msgData.handlers) {
            if (!Module2[handler] || Module2[handler].proxy) {
              Module2[handler] = (...args) => {
                postMessage({ cmd: 9, handler, args });
              };
              if (handler == "print")
                out = Module2[handler];
              if (handler == "printErr")
                err = Module2[handler];
            }
          }
          wasmMemory = msgData.wasmMemory;
          updateMemoryViews();
          Module2["wasm"] = msgData.wasmModule;
          loadModule();
        } else if (cmd == 2) {
          establishStackSpace(msgData.pthread_ptr);
          __emscripten_thread_init(msgData.pthread_ptr, 0, 0, 1, 0, 0);
          PThread.threadInitTLS();
          __emscripten_thread_mailbox_await(msgData.pthread_ptr);
          if (!initializedJS) {
            __embind_initialize_bindings();
            initializedJS = true;
          }
          try {
            invokeEntryPoint(msgData.start_routine, msgData.arg);
          } catch (ex) {
            if (ex != "unwind") {
              throw ex;
            }
          }
        } else if (cmd == 4) {
          if (initializedJS) {
            checkMailbox();
          }
        } else if (cmd) {
          err(`worker: received unknown command ${cmd}`);
          err(msgData);
        }
      } catch (ex) {
        if (runtimeInitialized)
          __emscripten_thread_crashed();
        throw ex;
      }
    };
    var initializedJS = false;
    self.onunhandledrejection = (e) => {
      throw e.reason || e;
    };
    self.onmessage = handleMessage;
  }
  var runtimeInitialized = false;
  function getMemoryBuffer() {
    var firefoxMatch = globalThis.navigator?.userAgent?.match(/Firefox\/(\d+)/);
    if (!firefoxMatch || Number(firefoxMatch[1]) >= 154) {
      try {
        var b2 = wasmMemory.toResizableBuffer();
        growMemViews = () => {
        };
        return b2;
      } catch {
      }
    }
    return wasmMemory.buffer;
  }
  function updateMemoryViews() {
    if (HEAP8?.buffer?.growable)
      return;
    var b2 = getMemoryBuffer();
    HEAP8 = new Int8Array(b2);
    HEAP16 = new Int16Array(b2);
    HEAPU8 = new Uint8Array(b2);
    HEAPU16 = new Uint16Array(b2);
    HEAP32 = new Int32Array(b2);
    HEAPU32 = new Uint32Array(b2);
    HEAPF32 = new Float32Array(b2);
    HEAPF64 = new Float64Array(b2);
    HEAP64 = new BigInt64Array(b2);
    HEAPU64 = new BigUint64Array(b2);
  }
  function initMemory() {
    if (ENVIRONMENT_IS_PTHREAD) {
      return;
    }
    {
      var INITIAL_MEMORY = 33554432;
      wasmMemory = new WebAssembly.Memory({ initial: INITIAL_MEMORY / 65536, maximum: 32768, shared: true });
    }
    updateMemoryViews();
  }
  initMemory();
  var HEAP8;
  var terminateWorker = (worker) => {
    worker.terminate();
    worker.onmessage = (e) => {
    };
  };
  var cleanupThread = (pthread_ptr) => {
    var worker = PThread.pthreads[pthread_ptr];
    PThread.returnWorkerToPool(worker);
  };
  var callRuntimeCallbacks = (callbacks) => {
    while (callbacks.length > 0) {
      callbacks.shift()(Module2);
    }
  };
  var onPreRuns = [];
  var spawnThread = (threadParams) => {
    var worker = PThread.getNewWorker();
    if (!worker) {
      return 6;
    }
    PThread.pthreads[threadParams.pthread_ptr] = worker;
    worker.pthread_ptr = threadParams.pthread_ptr;
    var msg = { cmd: 2, start_routine: threadParams.startRoutine, arg: threadParams.arg, pthread_ptr: threadParams.pthread_ptr };
    worker.postMessage(msg, threadParams.transferList);
    return 0;
  };
  var stackSave = () => _emscripten_stack_get_current();
  var stackRestore = (val) => __emscripten_stack_restore(val);
  var stackAlloc = (sz) => __emscripten_stack_alloc(sz);
  var HEAPF64;
  var HEAP64;
  var proxyToMainThread = (funcIndex, emAsmAddr, proxyMode, ...callArgs) => {
    var bufSize = 8 * callArgs.length * 2;
    var sp = stackSave();
    var args = stackAlloc(bufSize);
    var b2 = args >> 3;
    for (var arg of callArgs) {
      if (typeof arg == "bigint") {
        (growMemViews(), HEAP64)[b2++] = 1n;
        (growMemViews(), HEAP64)[b2++] = arg;
      } else {
        (growMemViews(), HEAP64)[b2++] = 0n;
        (growMemViews(), HEAPF64)[b2++] = arg;
      }
    }
    var rtn = __emscripten_run_js_on_main_thread(funcIndex, emAsmAddr, bufSize, args, proxyMode);
    stackRestore(sp);
    return rtn;
  };
  function _proc_exit(code) {
    if (ENVIRONMENT_IS_PTHREAD)
      return proxyToMainThread(0, 0, 1, code);
    throw `exit(${code})`;
  }
  var _exit = _proc_exit;
  var waitAsyncPolyfilled = !Atomics.waitAsync || globalThis.navigator?.userAgent && Number((navigator.userAgent.match(/Chrom(e|ium)\/([0-9]+)\./) || [])[2]) < 91;
  var HEAP32;
  var PThread = { unusedWorkers: [], tlsInitFunctions: [], pthreads: {}, init() {
    if (!ENVIRONMENT_IS_PTHREAD) {
      PThread.initMainThread();
    }
  }, initMainThread() {
    var pthreadPoolSize = moduleArg.__poolSize | 0;
    while (pthreadPoolSize--) {
      PThread.allocateUnusedWorker();
    }
  }, terminateAllThreads: () => {
    for (var worker of Object.values(PThread.pthreads)) {
      terminateWorker(worker);
    }
    for (var worker of PThread.unusedWorkers) {
      terminateWorker(worker);
    }
    PThread.unusedWorkers = [];
    PThread.pthreads = {};
  }, clearMailboxAwait: (pthread_ptr) => {
    if (!waitAsyncPolyfilled) {
      Atomics.notify((growMemViews(), HEAP32), pthread_ptr >> 2);
    }
  }, terminateRuntime: () => {
    PThread.terminateAllThreads();
    var pthread_ptr = _pthread_self();
    ___set_thread_state(0, 0, 0, 1);
    PThread.clearMailboxAwait(pthread_ptr);
  }, returnWorkerToPool: (worker) => {
    var pthread_ptr = worker.pthread_ptr;
    delete PThread.pthreads[pthread_ptr];
    PThread.unusedWorkers.push(worker);
    worker.pthread_ptr = 0;
    PThread.clearMailboxAwait(pthread_ptr);
    __emscripten_thread_free_data(pthread_ptr);
  }, threadInitTLS() {
    PThread.tlsInitFunctions.forEach((f) => f());
  }, loadWasmModuleToWorker: (worker) => new Promise((onFinishedLoading) => {
    worker.onmessage = (e) => {
      var d2 = e.data;
      var cmd = d2.cmd;
      if (d2.targetThread && d2.targetThread != _pthread_self()) {
        var targetWorker = PThread.pthreads[d2.targetThread];
        targetWorker?.postMessage(d2);
        return;
      }
      if (d2 === "setimmediate" || d2 === "_si") {
        worker.postMessage(d2);
        return;
      }
      switch (cmd) {
        case 4:
          checkMailbox();
          break;
        case 5:
          spawnThread(d2);
          break;
        case 6:
          callUserCallback(() => cleanupThread(d2.thread));
          break;
        case 3:
          onFinishedLoading(worker);
          break;
        case 9:
          Module2[d2.handler](...d2.args);
          break;
        default:
          if (cmd)
            err(`worker sent an unknown command ${cmd}`);
      }
    };
    worker.onerror = (e) => {
      var message = "worker sent an error!";
      err(`${message} ${e.filename}:${e.lineno}: ${e.message}`);
      throw e;
    };
    var handlers = [];
    var knownHandlers = [];
    for (var handler of knownHandlers) {
      if (Module2.propertyIsEnumerable(handler)) {
        handlers.push(handler);
      }
    }
    worker.postMessage({ cmd: 1, handlers, wasmMemory, wasmModule });
  }), async loadWasmModuleToAllWorkers() {
    if (ENVIRONMENT_IS_PTHREAD) {
      return;
    }
    let pthreadPoolReady = Promise.all(PThread.unusedWorkers.map(PThread.loadWasmModuleToWorker));
    return pthreadPoolReady;
  }, allocateUnusedWorker() {
    var worker;
    worker = new Worker(new URL("jassub-worker.js", import.meta.url), { type: "module", name: "em-pthread" });
    PThread.unusedWorkers.push(worker);
    return worker;
  }, getNewWorker() {
    if (PThread.unusedWorkers.length == 0) {
      var newWorker = PThread.allocateUnusedWorker();
      PThread.loadWasmModuleToWorker(newWorker);
    }
    return PThread.unusedWorkers.pop();
  } };
  var HEAPU32;
  function establishStackSpace(pthread_ptr) {
    var stackHigh = (growMemViews(), HEAPU32)[pthread_ptr + 48 >> 2];
    var stackSize = (growMemViews(), HEAPU32)[pthread_ptr + 52 >> 2];
    var stackLow = stackHigh - stackSize;
    _emscripten_stack_set_limits(stackHigh, stackLow);
    stackRestore(stackHigh);
  }
  var wasmTableMirror = [];
  var getWasmTableEntry = (funcPtr) => {
    var func = wasmTableMirror[funcPtr];
    if (!func) {
      wasmTableMirror[funcPtr] = func = wasmTable.get(funcPtr);
    }
    return func;
  };
  var invokeEntryPoint = (ptr, arg) => {
    var result = getWasmTableEntry(ptr)(arg);
    function finish(result2) {
      __emscripten_thread_exit(result2);
    }
    finish(result);
  };
  var lengthBytesUTF8 = (str) => {
    var len = 0;
    for (var i = 0; i < str.length; ++i) {
      var c2 = str.charCodeAt(i);
      if (c2 <= 127) {
        len++;
      } else if (c2 <= 2047) {
        len += 2;
      } else if (c2 >= 55296 && c2 <= 57343) {
        len += 4;
        ++i;
      } else {
        len += 3;
      }
    }
    return len;
  };
  var stringToUTF8Array = (str, heap, outIdx, maxBytesToWrite) => {
    if (!(maxBytesToWrite > 0))
      return 0;
    var startIdx = outIdx;
    var endIdx = outIdx + maxBytesToWrite - 1;
    for (var i = 0; i < str.length; ++i) {
      var u = str.codePointAt(i);
      if (u <= 127) {
        if (outIdx >= endIdx)
          break;
        heap[outIdx++] = u;
      } else if (u <= 2047) {
        if (outIdx + 1 >= endIdx)
          break;
        heap[outIdx++] = 192 | u >> 6;
        heap[outIdx++] = 128 | u & 63;
      } else if (u <= 65535) {
        if (outIdx + 2 >= endIdx)
          break;
        heap[outIdx++] = 224 | u >> 12;
        heap[outIdx++] = 128 | u >> 6 & 63;
        heap[outIdx++] = 128 | u & 63;
      } else {
        if (outIdx + 3 >= endIdx)
          break;
        heap[outIdx++] = 240 | u >> 18;
        heap[outIdx++] = 128 | u >> 12 & 63;
        heap[outIdx++] = 128 | u >> 6 & 63;
        heap[outIdx++] = 128 | u & 63;
        i++;
      }
    }
    heap[outIdx] = 0;
    return outIdx - startIdx;
  };
  var HEAPU8;
  var stringToUTF8 = (str, outPtr, maxBytesToWrite) => stringToUTF8Array(str, (growMemViews(), HEAPU8), outPtr, maxBytesToWrite);
  var stringToNewUTF8 = (str) => {
    var size = lengthBytesUTF8(str) + 1;
    var ret = _malloc(size);
    if (ret)
      stringToUTF8(str, ret, size);
    return ret;
  };
  var wasmMemory;
  var UTF8Decoder = new TextDecoder();
  var findStringEnd = (heapOrArray, idx, maxBytesToRead, ignoreNul) => {
    var maxIdx = idx + maxBytesToRead;
    if (ignoreNul)
      return maxIdx;
    while (heapOrArray[idx] && !(idx >= maxIdx))
      ++idx;
    return idx;
  };
  var UTF8ToString = (ptr, maxBytesToRead, ignoreNul) => {
    if (!ptr)
      return "";
    var end = findStringEnd((growMemViews(), HEAPU8), ptr, maxBytesToRead, ignoreNul);
    return UTF8Decoder.decode((growMemViews(), HEAPU8).slice(ptr, end));
  };
  var ___assert_fail = (condition, filename, line, func) => abort(`Assertion failed: ${UTF8ToString(condition)}, at: ` + [filename ? UTF8ToString(filename) : "unknown filename", line, func ? UTF8ToString(func) : "unknown function"]);
  function pthreadCreateProxied(pthread_ptr, attr, startRoutine, arg) {
    if (ENVIRONMENT_IS_PTHREAD)
      return proxyToMainThread(1, 0, 1, pthread_ptr, attr, startRoutine, arg);
    return ___pthread_create_js(pthread_ptr, attr, startRoutine, arg);
  }
  var _emscripten_has_threading_support = () => !!globalThis.SharedArrayBuffer;
  var ___pthread_create_js = (pthread_ptr, attr, startRoutine, arg) => {
    if (!_emscripten_has_threading_support()) {
      return 6;
    }
    var transferList = [];
    var error = 0;
    if (ENVIRONMENT_IS_PTHREAD && (!transferList.length || error)) {
      return pthreadCreateProxied(pthread_ptr, attr, startRoutine, arg);
    }
    if (error)
      return error;
    var threadParams = { startRoutine, pthread_ptr, arg, transferList };
    if (ENVIRONMENT_IS_PTHREAD) {
      threadParams.cmd = 5;
      postMessage(threadParams, transferList);
      return 0;
    }
    return spawnThread(threadParams);
  };
  var SYSCALLS = { varargs: void 0, getStr(ptr) {
    var ret = UTF8ToString(ptr);
    return ret;
  } };
  function ___syscall_fcntl64(fd, cmd, varargs) {
    if (ENVIRONMENT_IS_PTHREAD)
      return proxyToMainThread(2, 0, 1, fd, cmd, varargs);
    SYSCALLS.varargs = varargs;
    return 0;
  }
  function ___syscall_getdents64(fd, dirp, count) {
    if (ENVIRONMENT_IS_PTHREAD)
      return proxyToMainThread(3, 0, 1, fd, dirp, count);
  }
  function ___syscall_ioctl(fd, op, varargs) {
    if (ENVIRONMENT_IS_PTHREAD)
      return proxyToMainThread(4, 0, 1, fd, op, varargs);
    SYSCALLS.varargs = varargs;
    return 0;
  }
  function ___syscall_openat(dirfd, path, flags, varargs) {
    if (ENVIRONMENT_IS_PTHREAD)
      return proxyToMainThread(5, 0, 1, dirfd, path, flags, varargs);
    SYSCALLS.varargs = varargs;
  }
  var __abort_js = () => abort("");
  var AsciiToString = (ptr) => {
    var str = "";
    while (1) {
      var ch = (growMemViews(), HEAPU8)[ptr++];
      if (!ch)
        return str;
      str += String.fromCharCode(ch);
    }
  };
  var awaitingDependencies = {};
  var registeredTypes = {};
  var typeDependencies = {};
  class BindingError extends Error {
    constructor(message) {
      super(message);
      this.name = "BindingError";
    }
  }
  var throwBindingError = (message) => {
    throw new BindingError(message);
  };
  function sharedRegisterType(rawType, registeredInstance, options = {}) {
    var name = registeredInstance.name;
    if (!rawType) {
      throwBindingError(`type "${name}" must have a positive integer typeid pointer`);
    }
    if (registeredTypes.hasOwnProperty(rawType)) {
      if (options.ignoreDuplicateRegistrations) {
        return;
      } else {
        throwBindingError(`Cannot register type '${name}' twice`);
      }
    }
    registeredTypes[rawType] = registeredInstance;
    delete typeDependencies[rawType];
    if (awaitingDependencies.hasOwnProperty(rawType)) {
      var callbacks = awaitingDependencies[rawType];
      delete awaitingDependencies[rawType];
      callbacks.forEach((cb) => cb());
    }
  }
  function registerType(rawType, registeredInstance, options = {}) {
    return sharedRegisterType(rawType, registeredInstance, options);
  }
  var HEAP16;
  var HEAPU16;
  var HEAPU64;
  var integerReadValueFromPointer = (name, width, signed) => {
    switch (width) {
      case 1:
        return signed ? (pointer) => (growMemViews(), HEAP8)[pointer] : (pointer) => (growMemViews(), HEAPU8)[pointer];
      case 2:
        return signed ? (pointer) => (growMemViews(), HEAP16)[pointer >> 1] : (pointer) => (growMemViews(), HEAPU16)[pointer >> 1];
      case 4:
        return signed ? (pointer) => (growMemViews(), HEAP32)[pointer >> 2] : (pointer) => (growMemViews(), HEAPU32)[pointer >> 2];
      case 8:
        return signed ? (pointer) => (growMemViews(), HEAP64)[pointer >> 3] : (pointer) => (growMemViews(), HEAPU64)[pointer >> 3];
      default:
        throw new TypeError(`invalid integer width (${width}): ${name}`);
    }
  };
  var __embind_register_bigint = (primitiveType, name, size, minRange, maxRange) => {
    name = AsciiToString(name);
    const isUnsignedType = minRange === 0n;
    let fromWireType = (value) => value;
    if (isUnsignedType) {
      const bitSize = size * 8;
      fromWireType = (value) => BigInt.asUintN(bitSize, value);
      maxRange = fromWireType(maxRange);
    }
    registerType(primitiveType, { name, fromWireType, toWireType: (destructors, value) => {
      if (typeof value == "number") {
        value = BigInt(value);
      }
      return value;
    }, readValueFromPointer: integerReadValueFromPointer(name, size, !isUnsignedType), destructorFunction: null });
  };
  var __embind_register_bool = (rawType, name, trueValue, falseValue) => {
    name = AsciiToString(name);
    registerType(rawType, { name, fromWireType: function(wt) {
      return !!wt;
    }, toWireType: function(destructors, o) {
      return o ? trueValue : falseValue;
    }, readValueFromPointer: function(pointer) {
      return this.fromWireType((growMemViews(), HEAPU8)[pointer]);
    }, destructorFunction: null });
  };
  var shallowCopyInternalPointer = (o) => ({ count: o.count, deleteScheduled: o.deleteScheduled, preservePointerOnDelete: o.preservePointerOnDelete, ptr: o.ptr, ptrType: o.ptrType, smartPtr: o.smartPtr, smartPtrType: o.smartPtrType });
  var throwInstanceAlreadyDeleted = (obj) => {
    function getInstanceTypeName(handle) {
      return handle.$$.ptrType.registeredClass.name;
    }
    throwBindingError(getInstanceTypeName(obj) + " instance already deleted");
  };
  var finalizationRegistry = false;
  var detachFinalizer = (handle) => {
  };
  var runDestructor = ($$) => {
    if ($$.smartPtr) {
      $$.smartPtrType.rawDestructor($$.smartPtr);
    } else {
      $$.ptrType.registeredClass.rawDestructor($$.ptr);
    }
  };
  var releaseClassHandle = ($$) => {
    $$.count.value -= 1;
    var toDelete = 0 === $$.count.value;
    if (toDelete) {
      runDestructor($$);
    }
  };
  var attachFinalizer = (handle) => {
    if (!globalThis.FinalizationRegistry) {
      attachFinalizer = (handle2) => handle2;
      return handle;
    }
    finalizationRegistry = new FinalizationRegistry((info) => {
      releaseClassHandle(info.$$);
    });
    attachFinalizer = (handle2) => {
      var $$ = handle2.$$;
      var hasSmartPtr = !!$$.smartPtr;
      if (hasSmartPtr) {
        var info = { $$ };
        finalizationRegistry.register(handle2, info, handle2);
      }
      return handle2;
    };
    detachFinalizer = (handle2) => finalizationRegistry.unregister(handle2);
    return attachFinalizer(handle);
  };
  var deletionQueue = [];
  var flushPendingDeletes = () => {
    while (deletionQueue.length) {
      var obj = deletionQueue.pop();
      obj.$$.deleteScheduled = false;
      obj["delete"]();
    }
  };
  var delayFunction;
  var init_ClassHandle = () => {
    let proto = ClassHandle.prototype;
    Object.assign(proto, { isAliasOf(other) {
      if (!(this instanceof ClassHandle)) {
        return false;
      }
      if (!(other instanceof ClassHandle)) {
        return false;
      }
      var leftClass = this.$$.ptrType.registeredClass;
      var left = this.$$.ptr;
      other.$$ = other.$$;
      var rightClass = other.$$.ptrType.registeredClass;
      var right = other.$$.ptr;
      while (leftClass.baseClass) {
        left = leftClass.upcast(left);
        leftClass = leftClass.baseClass;
      }
      while (rightClass.baseClass) {
        right = rightClass.upcast(right);
        rightClass = rightClass.baseClass;
      }
      return leftClass === rightClass && left === right;
    }, clone() {
      if (!this.$$.ptr) {
        throwInstanceAlreadyDeleted(this);
      }
      if (this.$$.preservePointerOnDelete) {
        this.$$.count.value += 1;
        return this;
      } else {
        var clone = attachFinalizer(Object.create(Object.getPrototypeOf(this), { $$: { value: shallowCopyInternalPointer(this.$$) } }));
        clone.$$.count.value += 1;
        clone.$$.deleteScheduled = false;
        return clone;
      }
    }, delete() {
      if (!this.$$.ptr) {
        throwInstanceAlreadyDeleted(this);
      }
      if (this.$$.deleteScheduled && !this.$$.preservePointerOnDelete) {
        throwBindingError("Object already scheduled for deletion");
      }
      detachFinalizer(this);
      releaseClassHandle(this.$$);
      if (!this.$$.preservePointerOnDelete) {
        this.$$.smartPtr = void 0;
        this.$$.ptr = void 0;
      }
    }, isDeleted() {
      return !this.$$.ptr;
    }, deleteLater() {
      if (!this.$$.ptr) {
        throwInstanceAlreadyDeleted(this);
      }
      if (this.$$.deleteScheduled && !this.$$.preservePointerOnDelete) {
        throwBindingError("Object already scheduled for deletion");
      }
      deletionQueue.push(this);
      if (deletionQueue.length === 1 && delayFunction) {
        delayFunction(flushPendingDeletes);
      }
      this.$$.deleteScheduled = true;
      return this;
    } });
    const symbolDispose = Symbol.dispose;
    if (symbolDispose) {
      proto[symbolDispose] = proto["delete"];
    }
  };
  function ClassHandle() {
  }
  var createNamedFunction = (name, func) => Object.defineProperty(func, "name", { value: name });
  var registeredPointers = {};
  var ensureOverloadTable = (proto, methodName, humanName) => {
    if (void 0 === proto[methodName].overloadTable) {
      var prevFunc = proto[methodName];
      proto[methodName] = function(...args) {
        if (!proto[methodName].overloadTable.hasOwnProperty(args.length)) {
          throwBindingError(`Function '${humanName}' called with an invalid number of arguments (${args.length}) - expects one of (${proto[methodName].overloadTable})!`);
        }
        return proto[methodName].overloadTable[args.length].apply(this, args);
      };
      proto[methodName].overloadTable = [];
      proto[methodName].overloadTable[prevFunc.argCount] = prevFunc;
    }
  };
  var exposePublicSymbol = (name, value, numArguments) => {
    if (Module2.hasOwnProperty(name)) {
      if (void 0 === numArguments || void 0 !== Module2[name].overloadTable && void 0 !== Module2[name].overloadTable[numArguments]) {
        throwBindingError(`Cannot register public name '${name}' twice`);
      }
      ensureOverloadTable(Module2, name, name);
      if (Module2[name].overloadTable.hasOwnProperty(numArguments)) {
        throwBindingError(`Cannot register multiple overloads of a function with the same number of arguments (${numArguments})!`);
      }
      Module2[name].overloadTable[numArguments] = value;
    } else {
      Module2[name] = value;
      Module2[name].argCount = numArguments;
    }
  };
  var char_0 = 48;
  var char_9 = 57;
  var makeLegalFunctionName = (name) => {
    name = name.replace(/[^a-zA-Z0-9_]/g, "$");
    var f = name.charCodeAt(0);
    if (f >= char_0 && f <= char_9) {
      return `_${name}`;
    }
    return name;
  };
  function RegisteredClass(name, constructor2, instancePrototype, rawDestructor, baseClass, getActualType, upcast, downcast) {
    this.name = name;
    this.constructor = constructor2;
    this.instancePrototype = instancePrototype;
    this.rawDestructor = rawDestructor;
    this.baseClass = baseClass;
    this.getActualType = getActualType;
    this.upcast = upcast;
    this.downcast = downcast;
    this.pureVirtualFunctions = [];
  }
  var upcastPointer = (ptr, ptrClass, desiredClass) => {
    while (ptrClass !== desiredClass) {
      if (!ptrClass.upcast) {
        throwBindingError(`Expected null or instance of ${desiredClass.name}, got an instance of ${ptrClass.name}`);
      }
      ptr = ptrClass.upcast(ptr);
      ptrClass = ptrClass.baseClass;
    }
    return ptr;
  };
  var embindRepr = (v) => {
    if (v === null) {
      return "null";
    }
    var t = typeof v;
    if (t === "object" || t === "array" || t === "function") {
      return v.toString();
    } else {
      return "" + v;
    }
  };
  function constNoSmartPtrRawPointerToWireType(destructors, handle) {
    if (handle === null) {
      if (this.isReference) {
        throwBindingError(`null is not a valid ${this.name}`);
      }
      return 0;
    }
    if (!handle.$$) {
      throwBindingError(`Cannot pass "${embindRepr(handle)}" as a ${this.name}`);
    }
    if (!handle.$$.ptr) {
      throwBindingError(`Cannot pass deleted object as a pointer of type ${this.name}`);
    }
    var handleClass = handle.$$.ptrType.registeredClass;
    var ptr = upcastPointer(handle.$$.ptr, handleClass, this.registeredClass);
    return ptr;
  }
  function genericPointerToWireType(destructors, handle) {
    var ptr;
    if (handle === null) {
      if (this.isReference) {
        throwBindingError(`null is not a valid ${this.name}`);
      }
      if (this.isSmartPointer) {
        ptr = this.rawConstructor();
        if (destructors !== null) {
          destructors.push(this.rawDestructor, ptr);
        }
        return ptr;
      } else {
        return 0;
      }
    }
    if (!handle || !handle.$$) {
      throwBindingError(`Cannot pass "${embindRepr(handle)}" as a ${this.name}`);
    }
    if (!handle.$$.ptr) {
      throwBindingError(`Cannot pass deleted object as a pointer of type ${this.name}`);
    }
    if (!this.isConst && handle.$$.ptrType.isConst) {
      throwBindingError(`Cannot convert argument of type ${handle.$$.smartPtrType ? handle.$$.smartPtrType.name : handle.$$.ptrType.name} to parameter type ${this.name}`);
    }
    var handleClass = handle.$$.ptrType.registeredClass;
    ptr = upcastPointer(handle.$$.ptr, handleClass, this.registeredClass);
    if (this.isSmartPointer) {
      if (void 0 === handle.$$.smartPtr) {
        throwBindingError("Passing raw pointer to smart pointer is illegal");
      }
      switch (this.sharingPolicy) {
        case 0:
          if (handle.$$.smartPtrType === this) {
            ptr = handle.$$.smartPtr;
          } else {
            throwBindingError(`Cannot convert argument of type ${handle.$$.smartPtrType ? handle.$$.smartPtrType.name : handle.$$.ptrType.name} to parameter type ${this.name}`);
          }
          break;
        case 1:
          ptr = handle.$$.smartPtr;
          break;
        case 2:
          if (handle.$$.smartPtrType === this) {
            ptr = handle.$$.smartPtr;
          } else {
            var clonedHandle = handle["clone"]();
            ptr = this.rawShare(ptr, Emval.toHandle(() => clonedHandle["delete"]()));
            if (destructors !== null) {
              destructors.push(this.rawDestructor, ptr);
            }
          }
          break;
        default:
          throwBindingError("Unsupported sharing policy");
      }
    }
    return ptr;
  }
  function nonConstNoSmartPtrRawPointerToWireType(destructors, handle) {
    if (handle === null) {
      if (this.isReference) {
        throwBindingError(`null is not a valid ${this.name}`);
      }
      return 0;
    }
    if (!handle.$$) {
      throwBindingError(`Cannot pass "${embindRepr(handle)}" as a ${this.name}`);
    }
    if (!handle.$$.ptr) {
      throwBindingError(`Cannot pass deleted object as a pointer of type ${this.name}`);
    }
    if (handle.$$.ptrType.isConst) {
      throwBindingError(`Cannot convert argument of type ${handle.$$.ptrType.name} to parameter type ${this.name}`);
    }
    var handleClass = handle.$$.ptrType.registeredClass;
    var ptr = upcastPointer(handle.$$.ptr, handleClass, this.registeredClass);
    return ptr;
  }
  function readPointer(pointer) {
    return this.fromWireType((growMemViews(), HEAPU32)[pointer >> 2]);
  }
  var downcastPointer = (ptr, ptrClass, desiredClass) => {
    if (ptrClass === desiredClass) {
      return ptr;
    }
    if (void 0 === desiredClass.baseClass) {
      return null;
    }
    var rv = downcastPointer(ptr, ptrClass, desiredClass.baseClass);
    if (rv === null) {
      return null;
    }
    return desiredClass.downcast(rv);
  };
  var registeredInstances = {};
  var getBasestPointer = (class_, ptr) => {
    if (ptr === void 0) {
      throwBindingError("ptr should not be undefined");
    }
    while (class_.baseClass) {
      ptr = class_.upcast(ptr);
      class_ = class_.baseClass;
    }
    return ptr;
  };
  var getInheritedInstance = (class_, ptr) => {
    ptr = getBasestPointer(class_, ptr);
    return registeredInstances[ptr];
  };
  class InternalError extends Error {
    constructor(message) {
      super(message);
      this.name = "InternalError";
    }
  }
  var throwInternalError = (message) => {
    throw new InternalError(message);
  };
  var makeClassHandle = (prototype, record) => {
    if (!record.ptrType || !record.ptr) {
      throwInternalError("makeClassHandle requires ptr and ptrType");
    }
    var hasSmartPtrType = !!record.smartPtrType;
    var hasSmartPtr = !!record.smartPtr;
    if (hasSmartPtrType !== hasSmartPtr) {
      throwInternalError("Both smartPtrType and smartPtr must be specified");
    }
    record.count = { value: 1 };
    return attachFinalizer(Object.create(prototype, { $$: { value: record, writable: true } }));
  };
  function RegisteredPointer_fromWireType(ptr) {
    var rawPointer = this.getPointee(ptr);
    if (!rawPointer) {
      this.destructor(ptr);
      return null;
    }
    var registeredInstance = getInheritedInstance(this.registeredClass, rawPointer);
    if (void 0 !== registeredInstance) {
      if (0 === registeredInstance.$$.count.value) {
        registeredInstance.$$.ptr = rawPointer;
        registeredInstance.$$.smartPtr = ptr;
        return registeredInstance["clone"]();
      } else {
        var rv = registeredInstance["clone"]();
        this.destructor(ptr);
        return rv;
      }
    }
    function makeDefaultHandle() {
      if (this.isSmartPointer) {
        return makeClassHandle(this.registeredClass.instancePrototype, { ptrType: this.pointeeType, ptr: rawPointer, smartPtrType: this, smartPtr: ptr });
      } else {
        return makeClassHandle(this.registeredClass.instancePrototype, { ptrType: this, ptr });
      }
    }
    var actualType = this.registeredClass.getActualType(rawPointer);
    var registeredPointerRecord = registeredPointers[actualType];
    if (!registeredPointerRecord) {
      return makeDefaultHandle.call(this);
    }
    var toType;
    if (this.isConst) {
      toType = registeredPointerRecord.constPointerType;
    } else {
      toType = registeredPointerRecord.pointerType;
    }
    var dp = downcastPointer(rawPointer, this.registeredClass, toType.registeredClass);
    if (dp === null) {
      return makeDefaultHandle.call(this);
    }
    if (this.isSmartPointer) {
      return makeClassHandle(toType.registeredClass.instancePrototype, { ptrType: toType, ptr: dp, smartPtrType: this, smartPtr: ptr });
    } else {
      return makeClassHandle(toType.registeredClass.instancePrototype, { ptrType: toType, ptr: dp });
    }
  }
  var init_RegisteredPointer = () => {
    Object.assign(RegisteredPointer.prototype, { getPointee(ptr) {
      if (this.rawGetPointee) {
        ptr = this.rawGetPointee(ptr);
      }
      return ptr;
    }, destructor(ptr) {
      this.rawDestructor?.(ptr);
    }, readValueFromPointer: readPointer, fromWireType: RegisteredPointer_fromWireType });
  };
  function RegisteredPointer(name, registeredClass, isReference, isConst, isSmartPointer, pointeeType, sharingPolicy, rawGetPointee, rawConstructor, rawShare, rawDestructor) {
    this.name = name;
    this.registeredClass = registeredClass;
    this.isReference = isReference;
    this.isConst = isConst;
    this.isSmartPointer = isSmartPointer;
    this.pointeeType = pointeeType;
    this.sharingPolicy = sharingPolicy;
    this.rawGetPointee = rawGetPointee;
    this.rawConstructor = rawConstructor;
    this.rawShare = rawShare;
    this.rawDestructor = rawDestructor;
    if (!isSmartPointer && registeredClass.baseClass === void 0) {
      if (isConst) {
        this.toWireType = constNoSmartPtrRawPointerToWireType;
        this.destructorFunction = null;
      } else {
        this.toWireType = nonConstNoSmartPtrRawPointerToWireType;
        this.destructorFunction = null;
      }
    } else {
      this.toWireType = genericPointerToWireType;
    }
  }
  var replacePublicSymbol = (name, value, numArguments) => {
    if (!Module2.hasOwnProperty(name)) {
      throwInternalError("Replacing nonexistent public symbol");
    }
    if (void 0 !== Module2[name].overloadTable && void 0 !== numArguments) {
      Module2[name].overloadTable[numArguments] = value;
    } else {
      Module2[name] = value;
      Module2[name].argCount = numArguments;
    }
  };
  var embind__requireFunction = (signature, rawFunction, isAsync = false) => {
    signature = AsciiToString(signature);
    function makeDynCaller() {
      var rtn = getWasmTableEntry(rawFunction);
      return rtn;
    }
    var fp = makeDynCaller();
    if (typeof fp != "function") {
      throwBindingError(`unknown function pointer with signature ${signature}: ${rawFunction}`);
    }
    return fp;
  };
  class UnboundTypeError extends Error {
  }
  var getTypeName = (type) => {
    var ptr = ___getTypeName(type);
    var rv = AsciiToString(ptr);
    _free(ptr);
    return rv;
  };
  var throwUnboundTypeError = (message, types) => {
    var unboundTypes = [];
    var seen = {};
    function visit(type) {
      if (seen[type]) {
        return;
      }
      if (registeredTypes[type]) {
        return;
      }
      if (typeDependencies[type]) {
        typeDependencies[type].forEach(visit);
        return;
      }
      unboundTypes.push(type);
      seen[type] = true;
    }
    types.forEach(visit);
    throw new UnboundTypeError(`${message}: ` + unboundTypes.map(getTypeName).join([", "]));
  };
  var whenDependentTypesAreResolved = (myTypes, dependentTypes, getTypeConverters) => {
    myTypes.forEach((type) => typeDependencies[type] = dependentTypes);
    function onComplete(typeConverters2) {
      var myTypeConverters = getTypeConverters(typeConverters2);
      if (myTypeConverters.length !== myTypes.length) {
        throwInternalError("Mismatched type converter count");
      }
      for (var i = 0; i < myTypes.length; ++i) {
        registerType(myTypes[i], myTypeConverters[i]);
      }
    }
    var typeConverters = new Array(dependentTypes.length);
    var unregisteredTypes = [];
    var registered = 0;
    for (let [i, dt] of dependentTypes.entries()) {
      if (registeredTypes.hasOwnProperty(dt)) {
        typeConverters[i] = registeredTypes[dt];
      } else {
        unregisteredTypes.push(dt);
        if (!awaitingDependencies.hasOwnProperty(dt)) {
          awaitingDependencies[dt] = [];
        }
        awaitingDependencies[dt].push(() => {
          typeConverters[i] = registeredTypes[dt];
          ++registered;
          if (registered === unregisteredTypes.length) {
            onComplete(typeConverters);
          }
        });
      }
    }
    if (0 === unregisteredTypes.length) {
      onComplete(typeConverters);
    }
  };
  var __embind_register_class = (rawType, rawPointerType, rawConstPointerType, baseClassRawType, getActualTypeSignature, getActualType, upcastSignature, upcast, downcastSignature, downcast, name, destructorSignature, rawDestructor) => {
    name = AsciiToString(name);
    getActualType = embind__requireFunction(getActualTypeSignature, getActualType);
    upcast &&= embind__requireFunction(upcastSignature, upcast);
    downcast &&= embind__requireFunction(downcastSignature, downcast);
    rawDestructor = embind__requireFunction(destructorSignature, rawDestructor);
    var legalFunctionName = makeLegalFunctionName(name);
    exposePublicSymbol(legalFunctionName, function() {
      throwUnboundTypeError(`Cannot construct ${name} due to unbound types`, [baseClassRawType]);
    });
    whenDependentTypesAreResolved([rawType, rawPointerType, rawConstPointerType], baseClassRawType ? [baseClassRawType] : [], (base) => {
      base = base[0];
      var baseClass;
      var basePrototype;
      if (baseClassRawType) {
        baseClass = base.registeredClass;
        basePrototype = baseClass.instancePrototype;
      } else {
        basePrototype = ClassHandle.prototype;
      }
      var constructor2 = createNamedFunction(name, function(...args) {
        if (Object.getPrototypeOf(this) !== instancePrototype) {
          throw new BindingError(`Use 'new' to construct ${name}`);
        }
        if (void 0 === registeredClass.constructor_body) {
          throw new BindingError(`${name} has no accessible constructor`);
        }
        var body = registeredClass.constructor_body[args.length];
        if (void 0 === body) {
          throw new BindingError(`Tried to invoke ctor of ${name} with invalid number of parameters (${args.length}) - expected (${Object.keys(registeredClass.constructor_body).toString()}) parameters instead!`);
        }
        return body.apply(this, args);
      });
      var instancePrototype = Object.create(basePrototype, { constructor: { value: constructor2 } });
      constructor2.prototype = instancePrototype;
      var registeredClass = new RegisteredClass(name, constructor2, instancePrototype, rawDestructor, baseClass, getActualType, upcast, downcast);
      if (registeredClass.baseClass) {
        registeredClass.baseClass.__derivedClasses ??= [];
        registeredClass.baseClass.__derivedClasses.push(registeredClass);
      }
      var referenceConverter = new RegisteredPointer(name, registeredClass, true, false, false);
      var pointerConverter = new RegisteredPointer(name + "*", registeredClass, false, false, false);
      var constPointerConverter = new RegisteredPointer(name + " const*", registeredClass, false, true, false);
      registeredPointers[rawType] = { pointerType: pointerConverter, constPointerType: constPointerConverter };
      replacePublicSymbol(legalFunctionName, constructor2);
      return [referenceConverter, pointerConverter, constPointerConverter];
    });
  };
  var heap32VectorToArray = (count, firstElement) => {
    var array = [];
    for (var i = 0; i < count; i++) {
      array.push((growMemViews(), HEAPU32)[firstElement + i * 4 >> 2]);
    }
    return array;
  };
  var runDestructors = (destructors) => {
    while (destructors.length) {
      var ptr = destructors.pop();
      var del = destructors.pop();
      del(ptr);
    }
  };
  function usesDestructorStack(argTypes) {
    for (var i = 1; i < argTypes.length; ++i) {
      if (argTypes[i] !== null && argTypes[i].destructorFunction === void 0) {
        return true;
      }
    }
    return false;
  }
  function argsUseStackAlloc(argTypes) {
    for (var i = 1; i < argTypes.length; ++i) {
      if (argTypes[i] !== null && argTypes[i].argStackAlloc) {
        return true;
      }
    }
    return false;
  }
  var InvokerFunctions = { ftfnnn: function anonymous(humanName, throwBindingError2, invoker, fn, runDestructors2, fromRetWire, toClassParamWire, toArg0Wire, toArg1Wire, toArg2Wire) {
    return function(arg0, arg1, arg2) {
      var arg0Wired = toArg0Wire(null, arg0);
      var arg1Wired = toArg1Wire(null, arg1);
      var arg2Wired = toArg2Wire(null, arg2);
      var rv = invoker(fn, arg0Wired, arg1Wired, arg2Wired);
      var ret = fromRetWire(rv);
      return ret;
    };
  }, tffn: function anonymous(humanName, throwBindingError2, invoker, fn, runDestructors2, fromRetWire, toClassParamWire) {
    return function() {
      var thisWired = toClassParamWire(null, this);
      invoker(fn, thisWired);
    };
  }, tffnn: function anonymous(humanName, throwBindingError2, invoker, fn, runDestructors2, fromRetWire, toClassParamWire, toArg0Wire) {
    return function(arg0) {
      var thisWired = toClassParamWire(null, this);
      var arg0Wired = toArg0Wire(null, arg0);
      invoker(fn, thisWired, arg0Wired);
    };
  }, tffnnnnn: function anonymous(humanName, throwBindingError2, invoker, fn, runDestructors2, fromRetWire, toClassParamWire, toArg0Wire, toArg1Wire, toArg2Wire, toArg3Wire) {
    return function(arg0, arg1, arg2, arg3) {
      var thisWired = toClassParamWire(null, this);
      var arg0Wired = toArg0Wire(null, arg0);
      var arg1Wired = toArg1Wire(null, arg1);
      var arg2Wired = toArg2Wire(null, arg2);
      var arg3Wired = toArg3Wire(null, arg3);
      invoker(fn, thisWired, arg0Wired, arg1Wired, arg2Wired, arg3Wired);
    };
  }, tffnnn: function anonymous(humanName, throwBindingError2, invoker, fn, runDestructors2, fromRetWire, toClassParamWire, toArg0Wire, toArg1Wire) {
    return function(arg0, arg1) {
      var thisWired = toClassParamWire(null, this);
      var arg0Wired = toArg0Wire(null, arg0);
      var arg1Wired = toArg1Wire(null, arg1);
      invoker(fn, thisWired, arg0Wired, arg1Wired);
    };
  }, ttfnn: function anonymous(humanName, throwBindingError2, invoker, fn, runDestructors2, fromRetWire, toClassParamWire, toArg0Wire) {
    return function(arg0) {
      var thisWired = toClassParamWire(null, this);
      var arg0Wired = toArg0Wire(null, arg0);
      var rv = invoker(fn, thisWired, arg0Wired);
      var ret = fromRetWire(rv);
      return ret;
    };
  }, tffnt: function anonymous(humanName, throwBindingError2, invoker, fn, runDestructors2, fromRetWire, toClassParamWire, toArg0Wire, arg0Wired_dtor) {
    return function(arg0) {
      var thisWired = toClassParamWire(null, this);
      var arg0Wired = toArg0Wire(null, arg0);
      invoker(fn, thisWired, arg0Wired);
      arg0Wired_dtor(arg0Wired);
    };
  }, tffntnn: function anonymous(humanName, throwBindingError2, invoker, fn, runDestructors2, fromRetWire, toClassParamWire, toArg0Wire, toArg1Wire, toArg2Wire, arg0Wired_dtor) {
    return function(arg0, arg1, arg2) {
      var thisWired = toClassParamWire(null, this);
      var arg0Wired = toArg0Wire(null, arg0);
      var arg1Wired = toArg1Wire(null, arg1);
      var arg2Wired = toArg2Wire(null, arg2);
      invoker(fn, thisWired, arg0Wired, arg1Wired, arg2Wired);
      arg0Wired_dtor(arg0Wired);
    };
  }, tffnnnn: function anonymous(humanName, throwBindingError2, invoker, fn, runDestructors2, fromRetWire, toClassParamWire, toArg0Wire, toArg1Wire, toArg2Wire) {
    return function(arg0, arg1, arg2) {
      var thisWired = toClassParamWire(null, this);
      var arg0Wired = toArg0Wire(null, arg0);
      var arg1Wired = toArg1Wire(null, arg1);
      var arg2Wired = toArg2Wire(null, arg2);
      invoker(fn, thisWired, arg0Wired, arg1Wired, arg2Wired);
    };
  }, ttfn: function anonymous(humanName, throwBindingError2, invoker, fn, runDestructors2, fromRetWire, toClassParamWire) {
    return function() {
      var thisWired = toClassParamWire(null, this);
      var rv = invoker(fn, thisWired);
      var ret = fromRetWire(rv);
      return ret;
    };
  }, ttfnnn: function anonymous(humanName, throwBindingError2, invoker, fn, runDestructors2, fromRetWire, toClassParamWire, toArg0Wire, toArg1Wire) {
    return function(arg0, arg1) {
      var thisWired = toClassParamWire(null, this);
      var arg0Wired = toArg0Wire(null, arg0);
      var arg1Wired = toArg1Wire(null, arg1);
      var rv = invoker(fn, thisWired, arg0Wired, arg1Wired);
      var ret = fromRetWire(rv);
      return ret;
    };
  } };
  function createJsInvokerSignature(argTypes, isClassMethodFunc, returns, isAsync) {
    const signature = [isClassMethodFunc ? "t" : "f", returns ? "t" : "f", isAsync ? "t" : "f"];
    for (let i = isClassMethodFunc ? 1 : 2; i < argTypes.length; ++i) {
      const arg = argTypes[i];
      let destructorSig = "";
      if (arg.argStackAlloc) {
        destructorSig = "s";
      } else if (arg.destructorFunction === void 0) {
        destructorSig = "u";
      } else if (arg.destructorFunction === null) {
        destructorSig = "n";
      } else {
        destructorSig = "t";
      }
      signature.push(destructorSig);
    }
    return signature.join("");
  }
  function craftInvokerFunction(humanName, argTypes, classType, cppInvokerFunc, cppTargetFunc, isAsync) {
    var argCount = argTypes.length;
    if (argCount < 2) {
      throwBindingError("argTypes array size mismatch! Must at least get return value and receiver (this) types!");
    }
    var isClassMethodFunc = argTypes[1] !== null && classType !== null;
    var needsDestructorStack = usesDestructorStack(argTypes);
    var argsNeedStack = argsUseStackAlloc(argTypes);
    var useStackFrame = argsNeedStack && !isAsync && !needsDestructorStack;
    if (argsNeedStack && !useStackFrame) {
      needsDestructorStack = true;
    }
    var returns = !argTypes[0].isVoid;
    var retType = argTypes[0];
    var instType = argTypes[1];
    var closureArgs = [humanName, throwBindingError, cppInvokerFunc, cppTargetFunc, runDestructors, retType.fromWireType.bind(retType), instType?.toWireType.bind(instType)];
    if (useStackFrame) {
      closureArgs.push(stackSave, stackRestore);
    }
    for (var i = 2; i < argCount; ++i) {
      var argType = argTypes[i];
      closureArgs.push(argType.toWireType.bind(argType));
    }
    if (!needsDestructorStack) {
      for (var i = isClassMethodFunc ? 1 : 2; i < argTypes.length; ++i) {
        if (argTypes[i].destructorFunction !== null) {
          closureArgs.push(argTypes[i].destructorFunction);
        }
      }
    }
    var signature = createJsInvokerSignature(argTypes, isClassMethodFunc, returns, isAsync);
    var invokerFn = InvokerFunctions[signature](...closureArgs);
    return createNamedFunction(humanName, invokerFn);
  }
  var __embind_register_class_constructor = (rawClassType, argCount, rawArgTypesAddr, invokerSignature, invoker, rawConstructor) => {
    var rawArgTypes = heap32VectorToArray(argCount, rawArgTypesAddr);
    invoker = embind__requireFunction(invokerSignature, invoker);
    whenDependentTypesAreResolved([], [rawClassType], (classType) => {
      classType = classType[0];
      var humanName = `constructor ${classType.name}`;
      if (void 0 === classType.registeredClass.constructor_body) {
        classType.registeredClass.constructor_body = [];
      }
      if (void 0 !== classType.registeredClass.constructor_body[argCount - 1]) {
        throw new BindingError(`Cannot register multiple constructors with identical number of parameters (${argCount - 1}) for class '${classType.name}'! Overload resolution is currently only performed using the parameter count, not actual type info!`);
      }
      classType.registeredClass.constructor_body[argCount - 1] = () => {
        throwUnboundTypeError(`Cannot construct ${classType.name} due to unbound types`, rawArgTypes);
      };
      whenDependentTypesAreResolved([], rawArgTypes, (argTypes) => {
        argTypes.splice(1, 0, null);
        classType.registeredClass.constructor_body[argCount - 1] = craftInvokerFunction(humanName, argTypes, null, invoker, rawConstructor);
        return [];
      });
      return [];
    });
  };
  var getFunctionName = (signature) => {
    signature = signature.trim();
    const argsIndex = signature.indexOf("(");
    if (argsIndex === -1)
      return signature;
    return signature.slice(0, argsIndex);
  };
  var __embind_register_class_function = (rawClassType, methodName, argCount, rawArgTypesAddr, invokerSignature, rawInvoker, context, isPureVirtual, isAsync, isNonnullReturn) => {
    var rawArgTypes = heap32VectorToArray(argCount, rawArgTypesAddr);
    methodName = AsciiToString(methodName);
    methodName = getFunctionName(methodName);
    rawInvoker = embind__requireFunction(invokerSignature, rawInvoker, isAsync);
    whenDependentTypesAreResolved([], [rawClassType], (classType) => {
      classType = classType[0];
      var humanName = `${classType.name}.${methodName}`;
      if (methodName.startsWith("@@")) {
        methodName = Symbol[methodName.substring(2)];
      }
      if (isPureVirtual) {
        classType.registeredClass.pureVirtualFunctions.push(methodName);
      }
      function unboundTypesHandler() {
        throwUnboundTypeError(`Cannot call ${humanName} due to unbound types`, rawArgTypes);
      }
      var proto = classType.registeredClass.instancePrototype;
      var method = proto[methodName];
      if (void 0 === method || void 0 === method.overloadTable && method.className !== classType.name && method.argCount === argCount - 2) {
        unboundTypesHandler.argCount = argCount - 2;
        unboundTypesHandler.className = classType.name;
        proto[methodName] = unboundTypesHandler;
      } else {
        ensureOverloadTable(proto, methodName, humanName);
        proto[methodName].overloadTable[argCount - 2] = unboundTypesHandler;
      }
      whenDependentTypesAreResolved([], rawArgTypes, (argTypes) => {
        var memberFunction = craftInvokerFunction(humanName, argTypes, classType, rawInvoker, context, isAsync);
        if (void 0 === proto[methodName].overloadTable) {
          memberFunction.argCount = argCount - 2;
          proto[methodName] = memberFunction;
        } else {
          proto[methodName].overloadTable[argCount - 2] = memberFunction;
        }
        return [];
      });
      return [];
    });
  };
  var validateThis = (this_, classType, humanName) => {
    if (!(this_ instanceof Object)) {
      throwBindingError(`${humanName} with invalid "this": ${this_}`);
    }
    if (!(this_ instanceof classType.registeredClass.constructor)) {
      throwBindingError(`${humanName} incompatible with "this" of type ${this_.constructor.name}`);
    }
    if (!this_.$$.ptr) {
      throwBindingError(`cannot call emscripten binding method ${humanName} on deleted object`);
    }
    return upcastPointer(this_.$$.ptr, this_.$$.ptrType.registeredClass, classType.registeredClass);
  };
  var __embind_register_class_property = (classType, fieldName, getterReturnType, getterSignature, getter, getterContext, setterArgumentType, setterSignature, setter, setterContext) => {
    fieldName = AsciiToString(fieldName);
    getter = embind__requireFunction(getterSignature, getter);
    whenDependentTypesAreResolved([], [classType], (classType2) => {
      classType2 = classType2[0];
      var humanName = `${classType2.name}.${fieldName}`;
      var desc = { get() {
        throwUnboundTypeError(`Cannot access ${humanName} due to unbound types`, [getterReturnType, setterArgumentType]);
      }, enumerable: true, configurable: true };
      if (setter) {
        desc.set = () => throwUnboundTypeError(`Cannot access ${humanName} due to unbound types`, [getterReturnType, setterArgumentType]);
      } else {
        desc.set = (v) => throwBindingError(humanName + " is a read-only property");
      }
      Object.defineProperty(classType2.registeredClass.instancePrototype, fieldName, desc);
      whenDependentTypesAreResolved([], setter ? [getterReturnType, setterArgumentType] : [getterReturnType], (types) => {
        var getterReturnType2 = types[0];
        var desc2 = { get() {
          var ptr = validateThis(this, classType2, humanName + " getter");
          return getterReturnType2.fromWireType(getter(getterContext, ptr));
        }, enumerable: true };
        if (setter) {
          setter = embind__requireFunction(setterSignature, setter);
          var setterArgumentType2 = types[1];
          desc2.set = function(v) {
            var ptr = validateThis(this, classType2, humanName + " setter");
            var destructors = [];
            setter(setterContext, ptr, setterArgumentType2.toWireType(destructors, v));
            runDestructors(destructors);
          };
        }
        Object.defineProperty(classType2.registeredClass.instancePrototype, fieldName, desc2);
        return [];
      });
      return [];
    });
  };
  var emval_freelist = [];
  var emval_handles = [0, 1, , 1, null, 1, true, 1, false, 1];
  var __emval_decref = (handle) => {
    if (handle > 9 && 0 === --emval_handles[handle + 1]) {
      var value = emval_handles[handle];
      emval_handles[handle] = void 0;
      emval_freelist.push(handle);
    }
  };
  var Emval = { toValue: (handle) => {
    if (!handle) {
      throwBindingError(`Cannot use deleted val. handle = ${handle}`);
    }
    return emval_handles[handle];
  }, toHandle: (value) => {
    switch (value) {
      case void 0:
        return 2;
      case null:
        return 4;
      case true:
        return 6;
      case false:
        return 8;
      default: {
        const handle = emval_freelist.pop() || emval_handles.length;
        emval_handles[handle] = value;
        emval_handles[handle + 1] = 1;
        return handle;
      }
    }
  } };
  var EmValType = { name: "emscripten::val", fromWireType: (handle) => {
    var rv = Emval.toValue(handle);
    __emval_decref(handle);
    return rv;
  }, toWireType: (destructors, value) => Emval.toHandle(value), readValueFromPointer: readPointer, destructorFunction: null };
  var __embind_register_emval = (rawType) => registerType(rawType, EmValType);
  var HEAPF32;
  var floatReadValueFromPointer = (name, width) => {
    switch (width) {
      case 4:
        return function(pointer) {
          return this.fromWireType((growMemViews(), HEAPF32)[pointer >> 2]);
        };
      case 8:
        return function(pointer) {
          return this.fromWireType((growMemViews(), HEAPF64)[pointer >> 3]);
        };
      default:
        throw new TypeError(`invalid float width (${width}): ${name}`);
    }
  };
  var __embind_register_float = (rawType, name, size) => {
    name = AsciiToString(name);
    registerType(rawType, { name, fromWireType: (value) => value, toWireType: (destructors, value) => value, readValueFromPointer: floatReadValueFromPointer(name, size), destructorFunction: null });
  };
  var __embind_register_integer = (primitiveType, name, size, minRange, maxRange) => {
    name = AsciiToString(name);
    const isUnsignedType = minRange === 0;
    let fromWireType = (value) => value;
    if (isUnsignedType) {
      var bitshift = 32 - 8 * size;
      fromWireType = (value) => value << bitshift >>> bitshift;
      maxRange = fromWireType(maxRange);
    }
    registerType(primitiveType, { name, fromWireType, toWireType: (destructors, value) => value, readValueFromPointer: integerReadValueFromPointer(name, size, minRange !== 0), destructorFunction: null });
  };
  var __embind_register_memory_view = (rawType, dataTypeIndex, name) => {
    var typeMapping = [Int8Array, Uint8Array, Int16Array, Uint16Array, Int32Array, Uint32Array, Float32Array, Float64Array, BigInt64Array, BigUint64Array];
    var TA = typeMapping[dataTypeIndex];
    function decodeMemoryView(handle) {
      var size = (growMemViews(), HEAPU32)[handle >> 2];
      var data = (growMemViews(), HEAPU32)[handle + 4 >> 2];
      return new TA((growMemViews(), HEAP8).buffer, data, size);
    }
    name = AsciiToString(name);
    registerType(rawType, { name, fromWireType: decodeMemoryView, readValueFromPointer: decodeMemoryView }, { ignoreDuplicateRegistrations: true });
  };
  var __embind_register_std_string = (rawType, name) => {
    name = AsciiToString(name);
    var stdStringIsUTF8 = true;
    registerType(rawType, { name, fromWireType(value) {
      var length = (growMemViews(), HEAPU32)[value >> 2];
      var payload = value + 4;
      var str;
      if (stdStringIsUTF8) {
        str = UTF8ToString(payload, length, true);
      } else {
        str = "";
        for (var i = 0; i < length; ++i) {
          str += String.fromCharCode((growMemViews(), HEAPU8)[payload + i]);
        }
      }
      _free(value);
      return str;
    }, toWireType(destructors, value) {
      if (value instanceof ArrayBuffer) {
        value = new Uint8Array(value);
      }
      var length;
      var valueIsOfTypeString = typeof value == "string";
      if (!(valueIsOfTypeString || ArrayBuffer.isView(value) && value.BYTES_PER_ELEMENT == 1)) {
        throwBindingError("Cannot pass non-string to std::string");
      }
      if (stdStringIsUTF8 && valueIsOfTypeString) {
        length = lengthBytesUTF8(value);
      } else {
        length = value.length;
      }
      var base = _malloc(4 + length + 1);
      var ptr = base + 4;
      (growMemViews(), HEAPU32)[base >> 2] = length;
      if (valueIsOfTypeString) {
        if (stdStringIsUTF8) {
          stringToUTF8(value, ptr, length + 1);
        } else {
          for (var i = 0; i < length; ++i) {
            var charCode = value.charCodeAt(i);
            if (charCode > 255) {
              _free(base);
              throwBindingError("String has UTF-16 code units that do not fit in 8 bits");
            }
            (growMemViews(), HEAPU8)[ptr + i] = charCode;
          }
        }
      } else {
        (growMemViews(), HEAPU8).set(value, ptr);
      }
      if (destructors !== null) {
        destructors.push(_free, base);
      }
      return base;
    }, readValueFromPointer: readPointer, destructorFunction(ptr) {
      _free(ptr);
    } });
  };
  var UTF16Decoder = new TextDecoder("utf-16le");
  var UTF16ToString = (ptr, maxBytesToRead, ignoreNul) => {
    var idx = ptr >> 1;
    var endIdx = findStringEnd((growMemViews(), HEAPU16), idx, maxBytesToRead / 2, ignoreNul);
    return UTF16Decoder.decode((growMemViews(), HEAPU16).slice(idx, endIdx));
  };
  var stringToUTF16 = (str, outPtr, maxBytesToWrite = 2147483647) => {
    if (maxBytesToWrite < 2)
      return 0;
    maxBytesToWrite -= 2;
    var startPtr = outPtr;
    var numCharsToWrite = maxBytesToWrite < str.length * 2 ? maxBytesToWrite / 2 : str.length;
    for (var i = 0; i < numCharsToWrite; ++i) {
      var codeUnit = str.charCodeAt(i);
      (growMemViews(), HEAP16)[outPtr >> 1] = codeUnit;
      outPtr += 2;
    }
    (growMemViews(), HEAP16)[outPtr >> 1] = 0;
    return outPtr - startPtr;
  };
  var lengthBytesUTF16 = (str) => str.length * 2;
  var UTF32ToString = (ptr, maxBytesToRead, ignoreNul) => {
    var str = "";
    var startIdx = ptr >> 2;
    for (var i = 0; !(i >= maxBytesToRead / 4); i++) {
      var utf32 = (growMemViews(), HEAPU32)[startIdx + i];
      if (!utf32 && !ignoreNul)
        break;
      str += String.fromCodePoint(utf32);
    }
    return str;
  };
  var stringToUTF32 = (str, outPtr, maxBytesToWrite = 2147483647) => {
    if (maxBytesToWrite < 4)
      return 0;
    var startPtr = outPtr;
    var endPtr = startPtr + maxBytesToWrite - 4;
    for (var i = 0; i < str.length; ++i) {
      var codePoint = str.codePointAt(i);
      if (codePoint > 65535) {
        i++;
      }
      (growMemViews(), HEAP32)[outPtr >> 2] = codePoint;
      outPtr += 4;
      if (outPtr + 4 > endPtr)
        break;
    }
    (growMemViews(), HEAP32)[outPtr >> 2] = 0;
    return outPtr - startPtr;
  };
  var lengthBytesUTF32 = (str) => {
    var len = 0;
    for (var i = 0; i < str.length; ++i) {
      var codePoint = str.codePointAt(i);
      if (codePoint > 65535) {
        i++;
      }
      len += 4;
    }
    return len;
  };
  var __embind_register_std_wstring = (rawType, charSize, name) => {
    name = AsciiToString(name);
    var decodeString, encodeString, lengthBytesUTF;
    if (charSize === 2) {
      decodeString = UTF16ToString;
      encodeString = stringToUTF16;
      lengthBytesUTF = lengthBytesUTF16;
    } else {
      decodeString = UTF32ToString;
      encodeString = stringToUTF32;
      lengthBytesUTF = lengthBytesUTF32;
    }
    registerType(rawType, { name, fromWireType: (value) => {
      var length = (growMemViews(), HEAPU32)[value >> 2];
      var str = decodeString(value + 4, length * charSize, true);
      _free(value);
      return str;
    }, toWireType: (destructors, value) => {
      if (!(typeof value == "string")) {
        throwBindingError(`Cannot pass non-string to C++ string type ${name}`);
      }
      var length = lengthBytesUTF(value);
      var ptr = _malloc(4 + length + charSize);
      (growMemViews(), HEAPU32)[ptr >> 2] = length / charSize;
      encodeString(value, ptr + 4, length + charSize);
      if (destructors !== null) {
        destructors.push(_free, ptr);
      }
      return ptr;
    }, readValueFromPointer: readPointer, destructorFunction(ptr) {
      _free(ptr);
    } });
  };
  var __embind_register_void = (rawType, name) => {
    name = AsciiToString(name);
    registerType(rawType, { isVoid: true, name, fromWireType: () => void 0, toWireType: (destructors, o) => void 0 });
  };
  var __emscripten_init_main_thread_js = (tb) => {
    var can_block = !ENVIRONMENT_IS_WEB;
    __emscripten_thread_init(tb, !ENVIRONMENT_IS_WORKER, 1, can_block, 262144, false);
    PThread.threadInitTLS();
  };
  var callUserCallback = (func, ...args) => {
    func(...args);
  };
  var __emscripten_thread_mailbox_await = (pthread_ptr) => {
    if (!waitAsyncPolyfilled) {
      var wait = Atomics.waitAsync((growMemViews(), HEAP32), pthread_ptr >> 2, pthread_ptr);
      wait.value.then(checkMailbox);
      var waitingAsync = pthread_ptr + 112;
      Atomics.store((growMemViews(), HEAP32), waitingAsync >> 2, 1);
    }
  };
  var checkMailbox = () => {
    var pthread_ptr = _pthread_self();
    if (!pthread_ptr)
      return;
    callUserCallback(() => {
      __emscripten_thread_mailbox_await(pthread_ptr);
      __emscripten_check_mailbox();
    });
  };
  var __emscripten_notify_mailbox_postmessage = (targetThread, currThreadId) => {
    if (targetThread == currThreadId) {
      setTimeout(checkMailbox);
    } else if (ENVIRONMENT_IS_PTHREAD) {
      postMessage({ targetThread, cmd: 4 });
    } else {
      var worker = PThread.pthreads[targetThread];
      if (!worker) {
        return;
      }
      worker.postMessage({ cmd: 4 });
    }
  };
  var proxiedJSCallArgs = [];
  var __emscripten_receive_on_main_thread_js = (funcIndex, emAsmAddr, callingThread, bufSize, args, ctx, ctxArgs) => {
    proxiedJSCallArgs.length = 0;
    var b2 = args >> 3;
    var end = args + bufSize >> 3;
    while (b2 < end) {
      var arg;
      if ((growMemViews(), HEAP64)[b2++]) {
        arg = (growMemViews(), HEAP64)[b2++];
      } else {
        arg = (growMemViews(), HEAPF64)[b2++];
      }
      proxiedJSCallArgs.push(arg);
    }
    var func = emAsmAddr ? ASM_CONSTS[emAsmAddr] : proxiedFunctionTable[funcIndex];
    PThread.currentProxiedOperationCallerThread = callingThread;
    var rtn = func(...proxiedJSCallArgs);
    PThread.currentProxiedOperationCallerThread = 0;
    if (ctx) {
      Promise.resolve(rtn).then((rtn2) => __emscripten_run_js_on_main_thread_done(ctx, ctxArgs, rtn2));
      return;
    }
    return rtn;
  };
  var __emscripten_runtime_keepalive_clear = () => {
  };
  var __emscripten_thread_cleanup = (thread) => {
    if (!ENVIRONMENT_IS_PTHREAD)
      cleanupThread(thread);
    else
      postMessage({ cmd: 6, thread });
  };
  var __emscripten_thread_set_strongref = (thread) => {
  };
  var __emscripten_throw_longjmp = () => {
    throw new EmscriptenSjLj();
  };
  var emval_methodCallers = [];
  var emval_addMethodCaller = (caller) => {
    var id = emval_methodCallers.length;
    emval_methodCallers.push(caller);
    return id;
  };
  var requireRegisteredType = (rawType, humanName) => {
    var impl = registeredTypes[rawType];
    if (void 0 === impl) {
      throwBindingError(`${humanName} has unknown type ${getTypeName(rawType)}`);
    }
    return impl;
  };
  var emval_lookupTypes = (argCount, argTypes) => {
    var a2 = new Array(argCount);
    for (var i = 0; i < argCount; ++i) {
      a2[i] = requireRegisteredType((growMemViews(), HEAPU32)[argTypes + i * 4 >> 2], `parameter ${i}`);
    }
    return a2;
  };
  var emval_returnValue = (toReturnWire, destructorsRef, handle) => {
    var destructors = [];
    var result = toReturnWire(destructors, handle);
    if (destructors.length) {
      (growMemViews(), HEAPU32)[destructorsRef >> 2] = Emval.toHandle(destructors);
    }
    return result;
  };
  var emval_symbols = {};
  var getStringOrSymbol = (address) => {
    var symbol = emval_symbols[address];
    if (symbol === void 0) {
      return AsciiToString(address);
    }
    return symbol;
  };
  var __emval_create_invoker = (argCount, argTypesPtr, kind) => {
    var GenericWireTypeSize = 8;
    var [retType, ...argTypes] = emval_lookupTypes(argCount, argTypesPtr);
    var toReturnWire = retType.toWireType.bind(retType);
    var argFromPtr = argTypes.map((type) => type.readValueFromPointer.bind(type));
    argCount--;
    var argN = new Array(argCount);
    var invokerFunction = (handle, methodName, destructorsRef, args) => {
      var offset = 0;
      for (var i = 0; i < argCount; ++i) {
        argN[i] = argFromPtr[i](args + offset);
        offset += GenericWireTypeSize;
      }
      var rv;
      switch (kind) {
        case 0:
          rv = Emval.toValue(handle).apply(null, argN);
          break;
        case 2:
          rv = Reflect.construct(Emval.toValue(handle), argN);
          break;
        case 3:
          rv = argN[0];
          break;
        case 1:
          rv = Emval.toValue(handle)[getStringOrSymbol(methodName)](...argN);
          break;
      }
      return emval_returnValue(toReturnWire, destructorsRef, rv);
    };
    var functionName = `methodCaller<(${argTypes.map((t) => t.name)}) => ${retType.name}>`;
    return emval_addMethodCaller(createNamedFunction(functionName, invokerFunction));
  };
  var __emval_get_property = (handle, key) => {
    handle = Emval.toValue(handle);
    key = Emval.toValue(key);
    return Emval.toHandle(handle[key]);
  };
  var __emval_incref = (handle) => {
    if (handle > 9) {
      emval_handles[handle + 1] += 1;
    }
  };
  var __emval_invoke = (caller, handle, methodName, destructorsRef, args) => emval_methodCallers[caller](handle, methodName, destructorsRef, args);
  var __emval_new_array = () => Emval.toHandle([]);
  var __emval_new_cstring = (v) => Emval.toHandle(getStringOrSymbol(v));
  var __emval_new_object = () => Emval.toHandle({});
  var __emval_run_destructors = (handle) => {
    var destructors = Emval.toValue(handle);
    runDestructors(destructors);
    __emval_decref(handle);
  };
  var __emval_set_property = (handle, key, value) => {
    handle = Emval.toValue(handle);
    key = Emval.toValue(key);
    value = Emval.toValue(value);
    handle[key] = value;
  };
  var timers = {};
  var _emscripten_get_now = () => performance.timeOrigin + performance.now();
  function __setitimer_js(which, timeout_ms) {
    if (ENVIRONMENT_IS_PTHREAD)
      return proxyToMainThread(6, 0, 1, which, timeout_ms);
    if (timers[which]) {
      clearTimeout(timers[which].id);
      delete timers[which];
    }
    if (!timeout_ms)
      return 0;
    var id = setTimeout(() => {
      delete timers[which];
      callUserCallback(__emscripten_timeout, which, _emscripten_get_now());
    }, timeout_ms);
    timers[which] = { id, timeout_ms };
    return 0;
  }
  var _emscripten_date_now = () => Date.now();
  var INT53_MAX = 9007199254740992;
  var INT53_MIN = -9007199254740992;
  var bigintToI53Checked = (num) => num < INT53_MIN || num > INT53_MAX ? NaN : Number(num);
  var readEmAsmArgsArray = [];
  var readEmAsmArgs = (sigPtr, buf) => {
    readEmAsmArgsArray.length = 0;
    var ch;
    while (ch = (growMemViews(), HEAPU8)[sigPtr++]) {
      var wide = ch != 105;
      wide &= ch != 112;
      buf += wide && buf % 8 ? 4 : 0;
      readEmAsmArgsArray.push(ch == 112 ? (growMemViews(), HEAPU32)[buf >> 2] : ch == 106 ? (growMemViews(), HEAP64)[buf >> 3] : ch == 105 ? (growMemViews(), HEAP32)[buf >> 2] : (growMemViews(), HEAPF64)[buf >> 3]);
      buf += wide ? 8 : 4;
    }
    return readEmAsmArgsArray;
  };
  var runEmAsmFunction = (code, sigPtr, argbuf) => {
    var args = readEmAsmArgs(sigPtr, argbuf);
    return ASM_CONSTS[code](...args);
  };
  var _emscripten_asm_const_ptr = (code, sigPtr, argbuf) => runEmAsmFunction(code, sigPtr, argbuf);
  var _emscripten_check_blocking_allowed = () => {
  };
  var _emscripten_err = (str) => err(UTF8ToString(str));
  var _emscripten_exit_with_live_runtime = () => {
    throw "unwind";
  };
  var getHeapMax = () => 2147483648;
  var _emscripten_get_heap_max = () => getHeapMax();
  var _emscripten_num_logical_cores = () => navigator["hardwareConcurrency"];
  var alignMemory = (size, alignment) => Math.ceil(size / alignment) * alignment;
  var growMemory = (size) => {
    var oldHeapSize = wasmMemory.buffer.byteLength;
    var pages = (size - oldHeapSize + 65535) / 65536 | 0;
    try {
      wasmMemory.grow(pages);
      updateMemoryViews();
      return 1;
    } catch (e) {
    }
  };
  var _emscripten_resize_heap = (requestedSize) => {
    var oldSize = (growMemViews(), HEAPU8).length;
    requestedSize >>>= 0;
    if (requestedSize <= oldSize) {
      return false;
    }
    var maxHeapSize = getHeapMax();
    if (requestedSize > maxHeapSize) {
      return false;
    }
    for (var cutDown = 1; cutDown <= 4; cutDown *= 2) {
      var overGrownHeapSize = oldSize * (1 + 0.2 / cutDown);
      overGrownHeapSize = Math.min(overGrownHeapSize, requestedSize + 100663296);
      var newSize = Math.min(maxHeapSize, alignMemory(Math.max(requestedSize, overGrownHeapSize), 65536));
      var replacement = growMemory(newSize);
      if (replacement) {
        return true;
      }
    }
    return false;
  };
  var ENV = {};
  var getExecutableName = () => "./this.program";
  var getEnvStrings = () => {
    if (!getEnvStrings.strings) {
      var lang = (globalThis.navigator?.language ?? "C").replace("-", "_") + ".UTF-8";
      var env = { USER: "web_user", LOGNAME: "web_user", PATH: "/", PWD: "/", HOME: "/home/web_user", LANG: lang, _: getExecutableName() };
      for (var x in ENV) {
        if (ENV[x] === void 0)
          delete env[x];
        else
          env[x] = ENV[x];
      }
      var strings = [];
      for (var x in env) {
        strings.push(`${x}=${env[x]}`);
      }
      getEnvStrings.strings = strings;
    }
    return getEnvStrings.strings;
  };
  function _environ_get(__environ, environ_buf) {
    if (ENVIRONMENT_IS_PTHREAD)
      return proxyToMainThread(7, 0, 1, __environ, environ_buf);
    var bufSize = 0;
    var envp = 0;
    for (var string of getEnvStrings()) {
      var ptr = environ_buf + bufSize;
      (growMemViews(), HEAPU32)[__environ + envp >> 2] = ptr;
      bufSize += stringToUTF8(string, ptr, Infinity) + 1;
      envp += 4;
    }
    return 0;
  }
  function _environ_sizes_get(penviron_count, penviron_buf_size) {
    if (ENVIRONMENT_IS_PTHREAD)
      return proxyToMainThread(8, 0, 1, penviron_count, penviron_buf_size);
    var strings = getEnvStrings();
    (growMemViews(), HEAPU32)[penviron_count >> 2] = strings.length;
    var bufSize = 0;
    for (var string of strings) {
      bufSize += lengthBytesUTF8(string) + 1;
    }
    (growMemViews(), HEAPU32)[penviron_buf_size >> 2] = bufSize;
    return 0;
  }
  function _fd_close(fd) {
    if (ENVIRONMENT_IS_PTHREAD)
      return proxyToMainThread(9, 0, 1, fd);
    return 52;
  }
  function _fd_read(fd, iov, iovcnt, pnum) {
    if (ENVIRONMENT_IS_PTHREAD)
      return proxyToMainThread(10, 0, 1, fd, iov, iovcnt, pnum);
    return 52;
  }
  function _fd_seek(fd, offset, whence, newOffset) {
    if (ENVIRONMENT_IS_PTHREAD)
      return proxyToMainThread(11, 0, 1, fd, offset, whence, newOffset);
    offset = bigintToI53Checked(offset);
    return 70;
  }
  var printCharBuffers = [null, [], []];
  var UTF8ArrayToString = (heapOrArray, idx = 0, maxBytesToRead, ignoreNul) => {
    var endPtr = findStringEnd(heapOrArray, idx, maxBytesToRead, ignoreNul);
    return UTF8Decoder.decode(heapOrArray.buffer ? heapOrArray.buffer instanceof ArrayBuffer ? heapOrArray.subarray(idx, endPtr) : heapOrArray.slice(idx, endPtr) : new Uint8Array(heapOrArray.slice(idx, endPtr)));
  };
  var printChar = (stream, curr) => {
    var buffer2 = printCharBuffers[stream];
    if (!curr || curr === 10) {
      (stream === 1 ? out : err)(UTF8ArrayToString(buffer2));
      buffer2.length = 0;
    } else {
      buffer2.push(curr);
    }
  };
  function _fd_write(fd, iov, iovcnt, pnum) {
    if (ENVIRONMENT_IS_PTHREAD)
      return proxyToMainThread(12, 0, 1, fd, iov, iovcnt, pnum);
    var num = 0;
    for (var i = 0; i < iovcnt; i++) {
      var ptr = (growMemViews(), HEAPU32)[iov >> 2];
      var len = (growMemViews(), HEAPU32)[iov + 4 >> 2];
      iov += 8;
      for (var j = 0; j < len; j++) {
        printChar(fd, (growMemViews(), HEAPU8)[ptr + j]);
      }
      num += len;
    }
    (growMemViews(), HEAPU32)[pnum >> 2] = num;
    return 0;
  }
  var initRandomFill = () => (view) => (view.set(crypto.getRandomValues(new Uint8Array(view.byteLength))), 0);
  var randomFill = (view) => (randomFill = initRandomFill())(view);
  var _random_get = (buffer2, size) => randomFill((growMemViews(), HEAPU8).subarray(buffer2, buffer2 + size));
  PThread.init();
  init_ClassHandle();
  init_RegisteredPointer();
  var proxiedFunctionTable = [_proc_exit, pthreadCreateProxied, ___syscall_fcntl64, ___syscall_getdents64, ___syscall_ioctl, ___syscall_openat, __setitimer_js, _environ_get, _environ_sizes_get, _fd_close, _fd_read, _fd_seek, _fd_write];
  var ASM_CONSTS = { 611597: ($0) => stringToNewUTF8(Emval.toValue($0)) };
  var __ZdlPvm, _pthread_self, __Znwm, _free, _malloc, _calloc, ___getTypeName, __embind_initialize_bindings, _emscripten_builtin_free, __emscripten_tls_init, __emscripten_thread_init, ___set_thread_state, __emscripten_thread_crashed, ___libc_free, _emscripten_builtin_malloc, ___libc_malloc, __emscripten_run_js_on_main_thread_done, __emscripten_run_js_on_main_thread, __emscripten_thread_free_data, __emscripten_thread_exit, __emscripten_timeout, __emscripten_check_mailbox, __ZdaPv, __ZdaPvm, __ZdlPv, __Znam, __ZnamSt11align_val_t, __ZnwmSt11align_val_t, ___libc_calloc, ___libc_realloc, _emscripten_builtin_calloc, _emscripten_builtin_realloc, _malloc_size, _malloc_usable_size, _reallocf, _setThrew, _emscripten_stack_set_limits, __emscripten_stack_restore, __emscripten_stack_alloc, _emscripten_stack_get_current, __indirect_function_table, wasmTable;
  function assignWasmExports(wasmExports) {
    __ZdlPvm = Module2["__ZdlPvm"] = wasmExports["ha"];
    _pthread_self = wasmExports["ia"];
    __Znwm = Module2["__Znwm"] = wasmExports["ja"];
    _free = wasmExports["ka"];
    _malloc = Module2["_malloc"] = wasmExports["la"];
    _calloc = Module2["_calloc"] = wasmExports["ma"];
    ___getTypeName = wasmExports["na"];
    __embind_initialize_bindings = wasmExports["oa"];
    _emscripten_builtin_free = Module2["_emscripten_builtin_free"] = wasmExports["pa"];
    __emscripten_tls_init = wasmExports["qa"];
    __emscripten_thread_init = wasmExports["sa"];
    ___set_thread_state = wasmExports["ta"];
    __emscripten_thread_crashed = wasmExports["ua"];
    ___libc_free = Module2["___libc_free"] = wasmExports["va"];
    _emscripten_builtin_malloc = Module2["_emscripten_builtin_malloc"] = wasmExports["wa"];
    ___libc_malloc = Module2["___libc_malloc"] = wasmExports["xa"];
    __emscripten_run_js_on_main_thread_done = wasmExports["ya"];
    __emscripten_run_js_on_main_thread = wasmExports["za"];
    __emscripten_thread_free_data = wasmExports["Aa"];
    __emscripten_thread_exit = wasmExports["Ba"];
    __emscripten_timeout = wasmExports["Ca"];
    __emscripten_check_mailbox = wasmExports["Da"];
    __ZdaPv = Module2["__ZdaPv"] = wasmExports["Ea"];
    __ZdaPvm = Module2["__ZdaPvm"] = wasmExports["Fa"];
    __ZdlPv = Module2["__ZdlPv"] = wasmExports["Ga"];
    __Znam = Module2["__Znam"] = wasmExports["Ha"];
    __ZnamSt11align_val_t = Module2["__ZnamSt11align_val_t"] = wasmExports["Ia"];
    __ZnwmSt11align_val_t = Module2["__ZnwmSt11align_val_t"] = wasmExports["Ja"];
    ___libc_calloc = Module2["___libc_calloc"] = wasmExports["Ka"];
    ___libc_realloc = Module2["___libc_realloc"] = wasmExports["La"];
    _emscripten_builtin_calloc = Module2["_emscripten_builtin_calloc"] = wasmExports["Ma"];
    _emscripten_builtin_realloc = Module2["_emscripten_builtin_realloc"] = wasmExports["Na"];
    _malloc_size = Module2["_malloc_size"] = wasmExports["Oa"];
    _malloc_usable_size = Module2["_malloc_usable_size"] = wasmExports["Pa"];
    _reallocf = Module2["_reallocf"] = wasmExports["Qa"];
    _setThrew = wasmExports["Ra"];
    _emscripten_stack_set_limits = wasmExports["Sa"];
    __emscripten_stack_restore = wasmExports["Ta"];
    __emscripten_stack_alloc = wasmExports["Ua"];
    _emscripten_stack_get_current = wasmExports["Va"];
    __indirect_function_table = wasmTable = wasmExports["ra"];
  }
  var wasmImports;
  function assignWasmImports() {
    wasmImports = { b: ___assert_fail, P: ___pthread_create_js, y: ___syscall_fcntl64, O: ___syscall_getdents64, X: ___syscall_ioctl, z: ___syscall_openat, K: __abort_js, B: __embind_register_bigint, aa: __embind_register_bool, fa: __embind_register_class, ea: __embind_register_class_constructor, d: __embind_register_class_function, ca: __embind_register_class_property, _: __embind_register_emval, A: __embind_register_float, l: __embind_register_integer, e: __embind_register_memory_view, $: __embind_register_std_string, t: __embind_register_std_wstring, ba: __embind_register_void, S: __emscripten_init_main_thread_js, L: __emscripten_notify_mailbox_postmessage, x: __emscripten_receive_on_main_thread_js, G: __emscripten_runtime_keepalive_clear, v: __emscripten_thread_cleanup, R: __emscripten_thread_mailbox_await, Z: __emscripten_thread_set_strongref, E: __emscripten_throw_longjmp, h: __emval_create_invoker, c: __emval_decref, da: __emval_get_property, i: __emval_incref, g: __emval_invoke, p: __emval_new_array, j: __emval_new_cstring, s: __emval_new_object, f: __emval_run_destructors, k: __emval_set_property, H: __setitimer_js, n: _emscripten_asm_const_ptr, w: _emscripten_check_blocking_allowed, m: _emscripten_date_now, u: _emscripten_err, Y: _emscripten_exit_with_live_runtime, M: _emscripten_get_heap_max, o: _emscripten_get_now, N: _emscripten_num_logical_cores, I: _emscripten_resize_heap, T: _environ_get, U: _environ_sizes_get, q: _exit, r: _fd_close, W: _fd_read, Q: _fd_seek, V: _fd_write, D: invoke_iii, C: invoke_iiiii, a: wasmMemory, F: _proc_exit, J: _random_get };
  }
  function invoke_iii(index, a1, a2) {
    var sp = stackSave();
    try {
      return getWasmTableEntry(index)(a1, a2);
    } catch (e) {
      stackRestore(sp);
      if (!(e instanceof EmscriptenEH))
        throw e;
      _setThrew(1, 0);
    }
  }
  function invoke_iiiii(index, a1, a2, a3, a4) {
    var sp = stackSave();
    try {
      return getWasmTableEntry(index)(a1, a2, a3, a4);
    } catch (e) {
      stackRestore(sp);
      if (!(e instanceof EmscriptenEH))
        throw e;
      _setThrew(1, 0);
    }
  }
  function initRuntime(wasmExports) {
    runtimeInitialized = true;
    PThread.tlsInitFunctions.push(wasmExports["qa"]);
    if (ENVIRONMENT_IS_PTHREAD)
      return;
    wasmExports["ga"]();
  }
  var instantiatePromise;
  var wasmModule;
  function loadModule() {
    assignWasmImports();
    var imports = { a: wasmImports };
    instantiatePromise = WebAssembly.instantiateStreaming(fetch(new URL("jassub-worker.wasm", import.meta.url)), imports).then((output) => {
      var wasmExports = (output.instance || output).exports;
      wasmModule = output.module || Module2["wasm"];
      assignWasmExports(wasmExports);
      callRuntimeCallbacks(onPreRuns);
      initRuntime(wasmExports);
      return PThread.loadWasmModuleToAllWorkers().then(ready);
    });
  }
  if (!ENVIRONMENT_IS_PTHREAD) {
    loadModule();
  }
  await instantiatePromise;
  ;
  return Module2;
}
var jassub_worker_default = Module;
var isPthread = globalThis.name == "em-pthread";
isPthread && Module();

// ../../../../../../qa/lp-captions/jassub-build/jassub/dist/worker/util.js
var WEIGHT_MAP = [
  "thin",
  "extralight",
  "light",
  "regular",
  // Normal isn't used
  "medium",
  "semibold",
  "bold",
  "extrabold",
  "black",
  "ultrablack"
];
var UA = navigator.userAgent.toLowerCase();
var IS_FIREFOX = UA.includes("firefox");
var IS_SAFARI = /applewebkit\/[\d.]+ \(khtml, like gecko\)(?!.*chrome\/)/.test(UA);
var a = "BT601";
var b = "BT709";
var c = "SMPTE240M";
var d = "FCC";
var LIBASS_YCBCR_MAP = [null, a, null, a, a, b, b, c, c, d, d];
var _fetch = globalThis.fetch;
async function fetchtext(url) {
  const res = await _fetch(url);
  return await res.text();
}
var THREAD_COUNT = self.crossOriginIsolated ? Math.min(Math.max(1, navigator.hardwareConcurrency - 2), 8) : 1;
var SUPPORTS_GROWTH = !!WebAssembly.Memory.prototype.toResizableBuffer;
var SHOULD_REFERENCE_MEMORY = !IS_FIREFOX && !IS_SAFARI && (SUPPORTS_GROWTH || THREAD_COUNT > 1);
var IDENTITY_MATRIX = new Float32Array([
  1,
  0,
  0,
  0,
  1,
  0,
  0,
  0,
  1
]);
var colorMatrixConversionMap = {
  BT601: {
    BT709: new Float32Array([
      1.0863,
      0.0965,
      -0.01411,
      -0.0723,
      0.8451,
      -0.0277,
      -0.0141,
      0.0584,
      1.0418
    ]),
    BT601: IDENTITY_MATRIX
  },
  BT709: {
    BT601: new Float32Array([
      0.9137,
      0.0784,
      79e-4,
      -0.1049,
      1.1722,
      -0.0671,
      96e-4,
      0.0322,
      0.9582
    ]),
    BT709: IDENTITY_MATRIX
  },
  FCC: {
    BT709: new Float32Array([
      1.0873,
      -0.0736,
      -0.0137,
      0.0974,
      0.8494,
      0.0531,
      -0.0127,
      -0.0251,
      1.0378
    ]),
    BT601: new Float32Array([
      1.001,
      -8e-4,
      -2e-4,
      9e-4,
      1.005,
      -6e-3,
      13e-4,
      27e-4,
      0.996
    ])
  },
  SMPTE240M: {
    BT709: new Float32Array([
      0.9993,
      6e-4,
      1e-4,
      -4e-4,
      0.9812,
      0.0192,
      -34e-4,
      -0.0114,
      1.0148
    ]),
    BT601: new Float32Array([
      0.913,
      0.0774,
      96e-4,
      -0.1051,
      1.1508,
      -0.0456,
      63e-4,
      0.0207,
      0.973
    ])
  }
};

// ../../../../../../qa/lp-captions/jassub-build/jassub/dist/worker/renderers/2d-renderer.js
var clamp = (v) => v < 0 ? 0 : v > 1 ? 255 : Math.round(v * 255);
var Canvas2DRenderer = class {
  canvas = null;
  ctx = null;
  bufferCanvas = new OffscreenCanvas(1, 1);
  bufferCtx = this.bufferCanvas.getContext("2d", {
    alpha: true,
    desynchronized: true,
    willReadFrequently: false
  });
  colorMatrix = IDENTITY_MATRIX;
  _scheduledResize;
  resizeCanvas(width, height) {
    if (!width || !height)
      return;
    if (this.canvas?.width === width && this.canvas?.height === height)
      return;
    this._scheduledResize = { width, height };
  }
  setCanvas(canvas) {
    this.canvas = canvas;
    this.ctx = canvas.getContext("2d", {
      alpha: true,
      desynchronized: true,
      willReadFrequently: false
    });
    if (!this.ctx)
      throw new Error("Could not get 2D context");
  }
  // this should be an svg filter which is cheaper, but not supported
  // https://issues.chromium.org/u/1/issues/40910142
  setColorMatrix(subtitleColorSpace, videoColorSpace) {
    this.colorMatrix = (subtitleColorSpace && videoColorSpace && colorMatrixConversionMap[subtitleColorSpace]?.[videoColorSpace]) ?? IDENTITY_MATRIX;
  }
  // this is horribly inefficient, but it's a fallback for systems without a GPU, this is the least of their problems
  render(images, heap) {
    if (!this.ctx || !this.canvas)
      return;
    if (this._scheduledResize) {
      const { width, height } = this._scheduledResize;
      this._scheduledResize = void 0;
      this.canvas.width = width;
      this.canvas.height = height;
    }
    this.ctx.clearRect(0, 0, this.canvas.width, this.canvas.height);
    for (const img of images) {
      if (img.w <= 0 || img.h <= 0)
        continue;
      const imageData = new ImageData(img.w, img.h);
      const pixels = new Uint32Array(imageData.data.buffer);
      const m = this.colorMatrix;
      const r = (img.color >>> 24 & 255) / 255;
      const g = (img.color >>> 16 & 255) / 255;
      const bl = (img.color >>> 8 & 255) / 255;
      const cr = clamp(m[0] * r + m[3] * g + m[6] * bl);
      const cg = clamp(m[1] * r + m[4] * g + m[7] * bl);
      const cb = clamp(m[2] * r + m[5] * g + m[8] * bl);
      const color = cb << 16 | cg << 8 | cr;
      const alpha = (255 - (img.color & 255)) / 255;
      const stride = img.stride;
      const h = img.h;
      const w = img.w;
      for (let y = h + 1, pos = img.bitmap, res = 0; --y; pos += stride) {
        for (let z = 0; z < w; ++z, ++res) {
          const k = heap[pos + z];
          if (k !== 0)
            pixels[res] = Math.round(alpha * k) << 24 | color;
        }
      }
      this.bufferCanvas.width = w;
      this.bufferCanvas.height = h;
      this.bufferCtx.putImageData(imageData, 0, 0);
      this.ctx.drawImage(this.bufferCanvas, img.dst_x, img.dst_y);
    }
  }
  destroy() {
    this.ctx = null;
    this.canvas = null;
    this.bufferCtx = null;
    this.bufferCanvas = null;
  }
};

// ../../../../../../qa/lp-captions/jassub-build/jassub/dist/worker/renderers/webgl1-renderer.js
var VERTEX_SHADER = (
  /* glsl */
  `
precision mediump float;

// Quad position attribute (0,0), (1,0), (0,1), (1,0), (1,1), (0,1)
attribute vec2 a_quadPos;

uniform vec2 u_resolution;

// Instance attributes
attribute vec4 a_destRect;  // x, y, w, h
attribute vec4 a_color;     // r, g, b, a
attribute float a_texLayer;

varying vec2 v_destXY;
varying vec4 v_color;
varying vec2 v_texSize;
varying float v_texLayer;
varying vec2 v_texCoord;
varying vec2 v_normalizedImageSize;

uniform vec2 u_texDimensions;

void main() {
  vec2 pixelPos = a_destRect.xy + a_quadPos * a_destRect.zw;
  vec2 clipPos = (pixelPos / u_resolution) * 2.0 - 1.0;
  clipPos.y = -clipPos.y;

  gl_Position = vec4(clipPos, 0.0, 1.0);
  v_destXY = a_destRect.xy;
  v_color = a_color;
  v_texSize = a_destRect.zw;
  v_texLayer = a_texLayer;
  v_texCoord = a_quadPos;
  v_normalizedImageSize = a_destRect.zw / u_texDimensions;
}
`
);
var FRAGMENT_SHADER = (
  /* glsl */
  `
precision mediump float;

uniform sampler2D u_tex;
uniform mat3 u_colorMatrix;
uniform vec2 u_resolution;

varying vec2 v_destXY;
varying vec4 v_color;
varying vec2 v_texSize;
varying float v_texLayer;
varying vec2 v_texCoord;
varying vec2 v_normalizedImageSize;

void main() {
  // Sample texture (r channel contains mask)
  float mask = texture2D(u_tex, v_texCoord * v_normalizedImageSize).r;

  // Apply color matrix conversion (identity if no conversion needed)
  vec3 correctedColor = u_colorMatrix * v_color.rgb;

  // libass color alpha: 0 = opaque, 255 = transparent (inverted)
  float colorAlpha = 1.0 - v_color.a;

  // Final alpha = colorAlpha * mask
  float a = colorAlpha * mask;

  // Premultiplied alpha output
  gl_FragColor = vec4(correctedColor * a, a);
}
`
);
var MAX_INSTANCES = 256;
var WebGL1Renderer = class {
  canvas = null;
  gl = null;
  program = null;
  // Extensions
  instancedArraysExt = null;
  // Uniform locations
  u_resolution = null;
  u_tex = null;
  u_colorMatrix = null;
  u_texDimensions = null;
  // Attribute locations
  a_quadPos = -1;
  a_destRect = -1;
  a_color = -1;
  a_texLayer = -1;
  // Quad vertex buffer (shared for all instances)
  quadPosBuffer = null;
  // Instance attribute buffers
  instanceDestRectBuffer = null;
  instanceColorBuffer = null;
  instanceTexLayerBuffer = null;
  // Instance data arrays
  instanceDestRectData;
  instanceColorData;
  instanceTexLayerData;
  // Texture cache (since WebGL1 doesn't support texture arrays)
  textureCache = /* @__PURE__ */ new Map();
  // A reusable buffer for strided bitmap data
  tightData = null;
  colorMatrix = IDENTITY_MATRIX;
  constructor() {
    this.instanceDestRectData = new Float32Array(MAX_INSTANCES * 4);
    this.instanceColorData = new Float32Array(MAX_INSTANCES * 4);
    this.instanceTexLayerData = new Float32Array(MAX_INSTANCES);
  }
  _scheduledResize;
  resizeCanvas(width, height) {
    if (!width || !height)
      return;
    if (this.canvas?.width === width && this.canvas?.height === height)
      return;
    this._scheduledResize = { width, height };
  }
  setCanvas(canvas) {
    this._initGL(canvas);
    canvas.addEventListener("webglcontextlost", (e) => {
      e.preventDefault();
      this.gl = null;
    });
    canvas.addEventListener("webglcontextrestored", () => this._initGL(canvas));
  }
  _initGL(canvas) {
    this.canvas = canvas;
    this.gl = canvas.getContext("webgl", {
      alpha: true,
      premultipliedAlpha: true,
      antialias: false,
      depth: false,
      preserveDrawingBuffer: false,
      stencil: false,
      desynchronized: true,
      powerPreference: "default"
    });
    if (!this.gl) {
      throw new Error("Could not get WebGL context");
    }
    this.instancedArraysExt = this.gl.getExtension("ANGLE_instanced_arrays");
    if (!this.instancedArraysExt) {
      throw new Error("ANGLE_instanced_arrays extension not supported");
    }
    const vertexShader = this.createShader(this.gl.VERTEX_SHADER, VERTEX_SHADER);
    const fragmentShader = this.createShader(this.gl.FRAGMENT_SHADER, FRAGMENT_SHADER);
    if (!vertexShader || !fragmentShader) {
      throw new Error("Failed to create shaders");
    }
    this.program = this.gl.createProgram();
    this.gl.attachShader(this.program, vertexShader);
    this.gl.attachShader(this.program, fragmentShader);
    this.gl.linkProgram(this.program);
    if (!this.gl.getProgramParameter(this.program, this.gl.LINK_STATUS)) {
      const info = this.gl.getProgramInfoLog(this.program);
      throw new Error("Failed to link program: " + info);
    }
    this.u_resolution = this.gl.getUniformLocation(this.program, "u_resolution");
    this.u_tex = this.gl.getUniformLocation(this.program, "u_tex");
    this.u_colorMatrix = this.gl.getUniformLocation(this.program, "u_colorMatrix");
    this.u_texDimensions = this.gl.getUniformLocation(this.program, "u_texDimensions");
    this.a_quadPos = this.gl.getAttribLocation(this.program, "a_quadPos");
    this.a_destRect = this.gl.getAttribLocation(this.program, "a_destRect");
    this.a_color = this.gl.getAttribLocation(this.program, "a_color");
    this.a_texLayer = this.gl.getAttribLocation(this.program, "a_texLayer");
    this.quadPosBuffer = this.gl.createBuffer();
    this.gl.bindBuffer(this.gl.ARRAY_BUFFER, this.quadPosBuffer);
    const quadPositions = new Float32Array([
      0,
      0,
      1,
      0,
      0,
      1,
      1,
      0,
      1,
      1,
      0,
      1
    ]);
    this.gl.bufferData(this.gl.ARRAY_BUFFER, quadPositions, this.gl.STATIC_DRAW);
    this.instanceDestRectBuffer = this.gl.createBuffer();
    this.instanceColorBuffer = this.gl.createBuffer();
    this.instanceTexLayerBuffer = this.gl.createBuffer();
    this.gl.bindBuffer(this.gl.ARRAY_BUFFER, this.quadPosBuffer);
    this.gl.enableVertexAttribArray(this.a_quadPos);
    this.gl.vertexAttribPointer(this.a_quadPos, 2, this.gl.FLOAT, false, 0, 0);
    this.gl.bindBuffer(this.gl.ARRAY_BUFFER, this.instanceDestRectBuffer);
    this.gl.enableVertexAttribArray(this.a_destRect);
    this.gl.vertexAttribPointer(this.a_destRect, 4, this.gl.FLOAT, false, 0, 0);
    this.instancedArraysExt.vertexAttribDivisorANGLE(this.a_destRect, 1);
    this.gl.bindBuffer(this.gl.ARRAY_BUFFER, this.instanceColorBuffer);
    this.gl.enableVertexAttribArray(this.a_color);
    this.gl.vertexAttribPointer(this.a_color, 4, this.gl.FLOAT, false, 0, 0);
    this.instancedArraysExt.vertexAttribDivisorANGLE(this.a_color, 1);
    this.gl.bindBuffer(this.gl.ARRAY_BUFFER, this.instanceTexLayerBuffer);
    this.gl.enableVertexAttribArray(this.a_texLayer);
    this.gl.vertexAttribPointer(this.a_texLayer, 1, this.gl.FLOAT, false, 0, 0);
    this.instancedArraysExt.vertexAttribDivisorANGLE(this.a_texLayer, 1);
    this.gl.enable(this.gl.BLEND);
    this.gl.blendFunc(this.gl.ONE, this.gl.ONE_MINUS_SRC_ALPHA);
    this.gl.useProgram(this.program);
    this.gl.uniform1i(this.u_tex, 0);
    this.gl.uniformMatrix3fv(this.u_colorMatrix, false, this.colorMatrix);
    this.gl.pixelStorei(this.gl.UNPACK_ALIGNMENT, 1);
    this.gl.clearColor(0, 0, 0, 0);
    this.gl.activeTexture(this.gl.TEXTURE0);
    this.gl.viewport(0, 0, canvas.width, canvas.height);
    this.gl.uniform2f(this.u_resolution, canvas.width, canvas.height);
  }
  createShader(type, source) {
    const shader = this.gl.createShader(type);
    this.gl.shaderSource(shader, source);
    this.gl.compileShader(shader);
    if (!this.gl.getShaderParameter(shader, this.gl.COMPILE_STATUS)) {
      const info = this.gl.getShaderInfoLog(shader);
      console.log(info);
      this.gl.deleteShader(shader);
      return null;
    }
    return shader;
  }
  // Set the color matrix for color space conversion.
  // Pass null or undefined to use identity (no conversion).
  setColorMatrix(subtitleColorSpace, videoColorSpace) {
    this.colorMatrix = (subtitleColorSpace && videoColorSpace && colorMatrixConversionMap[subtitleColorSpace]?.[videoColorSpace]) ?? IDENTITY_MATRIX;
    if (this.gl && this.u_colorMatrix && this.program) {
      this.gl.useProgram(this.program);
      this.gl.uniformMatrix3fv(this.u_colorMatrix, false, this.colorMatrix);
    }
  }
  createTexture(width, height) {
    const texture = this.gl.createTexture();
    this.gl.bindTexture(this.gl.TEXTURE_2D, texture);
    this.gl.texImage2D(this.gl.TEXTURE_2D, 0, this.gl.LUMINANCE, width, height, 0, this.gl.LUMINANCE, this.gl.UNSIGNED_BYTE, null);
    this.gl.texParameteri(this.gl.TEXTURE_2D, this.gl.TEXTURE_MIN_FILTER, this.gl.NEAREST);
    this.gl.texParameteri(this.gl.TEXTURE_2D, this.gl.TEXTURE_MAG_FILTER, this.gl.NEAREST);
    this.gl.texParameteri(this.gl.TEXTURE_2D, this.gl.TEXTURE_WRAP_S, this.gl.CLAMP_TO_EDGE);
    this.gl.texParameteri(this.gl.TEXTURE_2D, this.gl.TEXTURE_WRAP_T, this.gl.CLAMP_TO_EDGE);
    return texture;
  }
  render(images, heap) {
    if (!this.gl || !this.program || !this.instancedArraysExt)
      return;
    if (this._scheduledResize) {
      const { width, height } = this._scheduledResize;
      this._scheduledResize = void 0;
      this.canvas.width = width;
      this.canvas.height = height;
      this.gl.viewport(0, 0, width, height);
      this.gl.uniform2f(this.u_resolution, width, height);
    }
    this.gl.clear(this.gl.COLOR_BUFFER_BIT);
    for (let i = 0; i < images.length; i++) {
      const img = images[i];
      if (img.w <= 0 || img.h <= 0)
        continue;
      let entry = this.textureCache.get(i);
      if (!entry || entry.width !== img.w || entry.height !== img.h) {
        if (entry)
          this.gl.deleteTexture(entry.texture);
        entry = { texture: this.createTexture(img.w, img.h), width: img.w, height: img.h };
        this.textureCache.set(i, entry);
      }
      this.gl.bindTexture(this.gl.TEXTURE_2D, entry.texture);
      if (img.stride === img.w) {
        this.gl.texSubImage2D(
          this.gl.TEXTURE_2D,
          0,
          0,
          0,
          // x, y offset
          img.w,
          img.h,
          this.gl.LUMINANCE,
          this.gl.UNSIGNED_BYTE,
          heap.subarray(img.bitmap, img.bitmap + img.w * img.h)
        );
      } else {
        const length = img.w * img.h;
        let tightData = this.tightData;
        if (!tightData || tightData.length < length)
          tightData = this.tightData = new Uint8Array(length);
        const sourceView = new Uint8Array(heap.buffer, img.bitmap, img.stride * img.h);
        for (let y = 0; y < img.h; y++) {
          const srcOffset = y * img.stride;
          tightData.set(sourceView.subarray(srcOffset, srcOffset + img.w), y * img.w);
        }
        this.gl.texSubImage2D(
          this.gl.TEXTURE_2D,
          0,
          0,
          0,
          // x, y offset
          img.w,
          img.h,
          this.gl.LUMINANCE,
          this.gl.UNSIGNED_BYTE,
          tightData.subarray(0, length)
        );
      }
      this.instanceDestRectData[0] = img.dst_x;
      this.instanceDestRectData[1] = img.dst_y;
      this.instanceDestRectData[2] = img.w;
      this.instanceDestRectData[3] = img.h;
      this.instanceColorData[0] = (img.color >>> 24 & 255) / 255;
      this.instanceColorData[1] = (img.color >>> 16 & 255) / 255;
      this.instanceColorData[2] = (img.color >>> 8 & 255) / 255;
      this.instanceColorData[3] = (img.color & 255) / 255;
      this.instanceTexLayerData[0] = 0;
      this.gl.bindBuffer(this.gl.ARRAY_BUFFER, this.instanceDestRectBuffer);
      this.gl.bufferData(this.gl.ARRAY_BUFFER, this.instanceDestRectData.subarray(0, 4), this.gl.DYNAMIC_DRAW);
      this.gl.bindBuffer(this.gl.ARRAY_BUFFER, this.instanceColorBuffer);
      this.gl.bufferData(this.gl.ARRAY_BUFFER, this.instanceColorData.subarray(0, 4), this.gl.DYNAMIC_DRAW);
      this.gl.bindBuffer(this.gl.ARRAY_BUFFER, this.instanceTexLayerBuffer);
      this.gl.bufferData(this.gl.ARRAY_BUFFER, this.instanceTexLayerData.subarray(0, 1), this.gl.DYNAMIC_DRAW);
      this.gl.uniform2f(this.u_texDimensions, img.w, img.h);
      this.instancedArraysExt.drawArraysInstancedANGLE(this.gl.TRIANGLES, 0, 6, 1);
    }
    this.gl.flush();
  }
  destroy() {
    if (this.gl) {
      for (const entry of this.textureCache.values()) {
        this.gl.deleteTexture(entry.texture);
      }
      this.textureCache.clear();
      this.tightData = null;
      if (this.quadPosBuffer) {
        this.gl.deleteBuffer(this.quadPosBuffer);
        this.quadPosBuffer = null;
      }
      if (this.instanceDestRectBuffer) {
        this.gl.deleteBuffer(this.instanceDestRectBuffer);
        this.instanceDestRectBuffer = null;
      }
      if (this.instanceColorBuffer) {
        this.gl.deleteBuffer(this.instanceColorBuffer);
        this.instanceColorBuffer = null;
      }
      if (this.instanceTexLayerBuffer) {
        this.gl.deleteBuffer(this.instanceTexLayerBuffer);
        this.instanceTexLayerBuffer = null;
      }
      if (this.program) {
        this.gl.deleteProgram(this.program);
        this.program = null;
      }
      this.gl = null;
    }
  }
};

// ../../../../../../qa/lp-captions/jassub-build/jassub/dist/worker/renderers/webgl2-renderer.js
var VERTEX_SHADER2 = (
  /* glsl */
  `#version 300 es
precision highp float;

const vec2 QUAD_POSITIONS[6] = vec2[6](
  vec2(0.0, 0.0),
  vec2(1.0, 0.0),
  vec2(0.0, 1.0),
  vec2(1.0, 0.0),
  vec2(1.0, 1.0),
  vec2(0.0, 1.0)
);

uniform vec2 u_resolution;

// Instance attributes
in vec4 a_destRect;  // x, y, w, h
in vec4 a_color;     // r, g, b, a

flat out vec2 v_destXY;
flat out vec4 v_color;
flat out vec2 v_texSize;
flat out float v_texLayer;

void main() {
  vec2 quadPos = QUAD_POSITIONS[gl_VertexID];
  vec2 pixelPos = a_destRect.xy + quadPos * a_destRect.zw;
  vec2 clipPos = (pixelPos / u_resolution) * 2.0 - 1.0;
  clipPos.y = -clipPos.y;

  gl_Position = vec4(clipPos, 0.0, 1.0);
  v_destXY = a_destRect.xy;
  v_color = a_color;
  v_texSize = a_destRect.zw;
  // The instance index in a batch equals its texture layer index.
  v_texLayer = float(gl_InstanceID);
}
`
);
var FRAGMENT_SHADER2 = (
  /* glsl */
  `#version 300 es
precision highp float;
precision highp sampler2DArray;

uniform sampler2DArray u_texArray;
uniform mat3 u_colorMatrix;
uniform vec2 u_resolution;

flat in vec2 v_destXY;
flat in vec4 v_color;
flat in vec2 v_texSize;
flat in float v_texLayer;

out vec4 fragColor;

void main() {
  // Flip Y: WebGL's gl_FragCoord.y is 0 at bottom, but destXY.y is from top
  vec2 fragPos = vec2(gl_FragCoord.x, u_resolution.y - gl_FragCoord.y);

  // Calculate local position within the quad (screen coords)
  vec2 localPos = fragPos - v_destXY;

  // Convert to integer texel coordinates for texelFetch
  ivec2 texCoord = ivec2(floor(localPos));

  // Bounds check (prevents out-of-bounds access)
  ivec2 texSizeI = ivec2(v_texSize);
  if (texCoord.x < 0 || texCoord.y < 0 || texCoord.x >= texSizeI.x || texCoord.y >= texSizeI.y) {
    discard;
  }

  // texelFetch: integer coords, no interpolation, no precision issues
  float mask = texelFetch(u_texArray, ivec3(texCoord, int(v_texLayer)), 0).r;

  // Apply color matrix conversion (identity if no conversion needed)
  vec3 correctedColor = u_colorMatrix * v_color.rgb;

  // libass color alpha: 0 = opaque, 255 = transparent (inverted)
  float colorAlpha = 1.0 - v_color.a;

  // Final alpha = colorAlpha * mask
  float a = colorAlpha * mask;

  // Premultiplied alpha output
  fragColor = vec4(correctedColor * a, a);
}
`
);
var TEX_ARRAY_SIZE = 64;
var TEX_INITIAL_SIZE = 256;
var MAX_INSTANCES2 = 256;
var INSTANCE_STRIDE = 20;
var WebGL2Renderer = class {
  canvas = null;
  gl = null;
  program = null;
  vao = null;
  // Uniform locations
  u_resolution = null;
  u_texArray = null;
  u_colorMatrix = null;
  // Interleaved instance buffer: dest rect (4 floats) then color (4 normalized bytes)
  instanceBuffer = null;
  instanceData;
  instanceFloats;
  instanceBytes;
  texArray = null;
  texArrayWidth = 0;
  texArrayHeight = 0;
  colorMatrix = IDENTITY_MATRIX;
  constructor() {
    this.instanceData = new ArrayBuffer(MAX_INSTANCES2 * INSTANCE_STRIDE);
    this.instanceFloats = new Float32Array(this.instanceData);
    this.instanceBytes = new Uint8Array(this.instanceData);
  }
  _scheduledResize;
  resizeCanvas(width, height) {
    if (!width || !height)
      return;
    if (this.canvas?.width === width && this.canvas?.height === height)
      return;
    this._scheduledResize = { width, height };
  }
  setCanvas(canvas) {
    this.canvas = canvas;
    this._initGL(canvas);
    canvas.addEventListener("webglcontextlost", (e) => {
      e.preventDefault();
      this.gl = null;
    });
    canvas.addEventListener("webglcontextrestored", () => this._initGL(canvas));
  }
  _initGL(canvas) {
    this.canvas = canvas;
    this.gl = canvas.getContext("webgl2", {
      alpha: true,
      premultipliedAlpha: true,
      antialias: false,
      depth: false,
      preserveDrawingBuffer: false,
      stencil: false,
      desynchronized: true,
      powerPreference: "default"
    });
    if (!this.gl) {
      throw new Error("Could not get WebGL2 context");
    }
    const vertexShader = this.createShader(this.gl.VERTEX_SHADER, VERTEX_SHADER2);
    const fragmentShader = this.createShader(this.gl.FRAGMENT_SHADER, FRAGMENT_SHADER2);
    if (!vertexShader || !fragmentShader) {
      throw new Error("Failed to create shaders");
    }
    this.program = this.gl.createProgram();
    this.gl.attachShader(this.program, vertexShader);
    this.gl.attachShader(this.program, fragmentShader);
    this.gl.linkProgram(this.program);
    if (!this.gl.getProgramParameter(this.program, this.gl.LINK_STATUS)) {
      const info = this.gl.getProgramInfoLog(this.program);
      throw new Error("Failed to link program: " + info);
    }
    this.gl.deleteShader(vertexShader);
    this.gl.deleteShader(fragmentShader);
    this.u_resolution = this.gl.getUniformLocation(this.program, "u_resolution");
    this.u_texArray = this.gl.getUniformLocation(this.program, "u_texArray");
    this.u_colorMatrix = this.gl.getUniformLocation(this.program, "u_colorMatrix");
    this.instanceBuffer = this.gl.createBuffer();
    this.vao = this.gl.createVertexArray();
    this.gl.bindVertexArray(this.vao);
    this.gl.bindBuffer(this.gl.ARRAY_BUFFER, this.instanceBuffer);
    this.gl.bufferData(this.gl.ARRAY_BUFFER, MAX_INSTANCES2 * INSTANCE_STRIDE, this.gl.DYNAMIC_DRAW);
    const destRectLoc = this.gl.getAttribLocation(this.program, "a_destRect");
    this.gl.enableVertexAttribArray(destRectLoc);
    this.gl.vertexAttribPointer(destRectLoc, 4, this.gl.FLOAT, false, INSTANCE_STRIDE, 0);
    this.gl.vertexAttribDivisor(destRectLoc, 1);
    const colorLoc = this.gl.getAttribLocation(this.program, "a_color");
    this.gl.enableVertexAttribArray(colorLoc);
    this.gl.vertexAttribPointer(colorLoc, 4, this.gl.UNSIGNED_BYTE, true, INSTANCE_STRIDE, 16);
    this.gl.vertexAttribDivisor(colorLoc, 1);
    this.gl.enable(this.gl.BLEND);
    this.gl.blendFunc(this.gl.ONE, this.gl.ONE_MINUS_SRC_ALPHA);
    this.gl.useProgram(this.program);
    this.gl.uniform1i(this.u_texArray, 0);
    this.gl.uniformMatrix3fv(this.u_colorMatrix, false, this.colorMatrix);
    this.gl.pixelStorei(this.gl.UNPACK_ALIGNMENT, 1);
    this.gl.clearColor(0, 0, 0, 0);
    this.gl.activeTexture(this.gl.TEXTURE0);
    this.gl.viewport(0, 0, canvas.width, canvas.height);
    this.gl.uniform2f(this.u_resolution, canvas.width, canvas.height);
    this.createTexArray(TEX_INITIAL_SIZE, TEX_INITIAL_SIZE);
  }
  createShader(type, source) {
    const shader = this.gl.createShader(type);
    this.gl.shaderSource(shader, source);
    this.gl.compileShader(shader);
    if (!this.gl.getShaderParameter(shader, this.gl.COMPILE_STATUS)) {
      const info = this.gl.getShaderInfoLog(shader);
      console.log(info);
      this.gl.deleteShader(shader);
      return null;
    }
    return shader;
  }
  // Set the color matrix for color space conversion.
  // Pass null or undefined to use identity (no conversion).
  setColorMatrix(subtitleColorSpace, videoColorSpace) {
    this.colorMatrix = (subtitleColorSpace && videoColorSpace && colorMatrixConversionMap[subtitleColorSpace]?.[videoColorSpace]) ?? IDENTITY_MATRIX;
    if (this.gl && this.u_colorMatrix && this.program) {
      this.gl.useProgram(this.program);
      this.gl.uniformMatrix3fv(this.u_colorMatrix, false, this.colorMatrix);
    }
  }
  createTexArray(width, height) {
    if (this.texArray) {
      this.gl.deleteTexture(this.texArray);
    }
    this.texArray = this.gl.createTexture();
    this.gl.bindTexture(this.gl.TEXTURE_2D_ARRAY, this.texArray);
    this.gl.texImage3D(
      this.gl.TEXTURE_2D_ARRAY,
      0,
      this.gl.R8,
      width,
      height,
      TEX_ARRAY_SIZE,
      0,
      this.gl.RED,
      this.gl.UNSIGNED_BYTE,
      null
      // Firefox cries about uninitialized data, but is slower with zero initialized data...
    );
    this.gl.texParameteri(this.gl.TEXTURE_2D_ARRAY, this.gl.TEXTURE_MIN_FILTER, this.gl.NEAREST);
    this.gl.texParameteri(this.gl.TEXTURE_2D_ARRAY, this.gl.TEXTURE_MAG_FILTER, this.gl.NEAREST);
    this.gl.texParameteri(this.gl.TEXTURE_2D_ARRAY, this.gl.TEXTURE_WRAP_S, this.gl.CLAMP_TO_EDGE);
    this.gl.texParameteri(this.gl.TEXTURE_2D_ARRAY, this.gl.TEXTURE_WRAP_T, this.gl.CLAMP_TO_EDGE);
    this.texArrayWidth = width;
    this.texArrayHeight = height;
  }
  render(images, heap) {
    if (!this.gl || !this.program || !this.vao || !this.texArray)
      return;
    if (self.HEAPU8RAW.buffer !== self.WASMMEMORY.buffer || SHOULD_REFERENCE_MEMORY) {
      heap = self.HEAPU8RAW = new Uint8Array(self.WASMMEMORY.buffer);
    }
    if (this._scheduledResize) {
      const { width, height } = this._scheduledResize;
      this._scheduledResize = void 0;
      this.canvas.width = width;
      this.canvas.height = height;
      this.gl.viewport(0, 0, width, height);
      this.gl.uniform2f(this.u_resolution, width, height);
    }
    this.gl.clear(this.gl.COLOR_BUFFER_BIT);
    let maxW = this.texArrayWidth;
    let maxH = this.texArrayHeight;
    const validImages = [];
    for (const img of images) {
      if (img.w <= 0 || img.h <= 0)
        continue;
      validImages.push(img);
      if (img.w > maxW)
        maxW = img.w;
      if (img.h > maxH)
        maxH = img.h;
    }
    if (validImages.length === 0) {
      this.gl.flush();
      return;
    }
    if (maxW > this.texArrayWidth || maxH > this.texArrayHeight) {
      this.createTexArray(maxW, maxH);
    }
    const batchSize = Math.min(TEX_ARRAY_SIZE, MAX_INSTANCES2);
    for (let batchStart = 0; batchStart < validImages.length; batchStart += batchSize) {
      const batchEnd = Math.min(batchStart + batchSize, validImages.length);
      let instanceCount = 0;
      for (let i = batchStart; i < batchEnd; i++) {
        const img = validImages[i];
        const layer = instanceCount;
        this.gl.pixelStorei(this.gl.UNPACK_ROW_LENGTH, img.stride);
        if (IS_FIREFOX || IS_SAFARI) {
          const sourceView = new Uint8Array(heap.buffer, img.bitmap, img.stride * img.h);
          const bitmapData = new Uint8Array(sourceView);
          this.gl.texSubImage3D(
            this.gl.TEXTURE_2D_ARRAY,
            0,
            0,
            0,
            layer,
            // x, y, z offset
            img.w,
            img.h,
            1,
            // depth (1 layer)
            this.gl.RED,
            this.gl.UNSIGNED_BYTE,
            bitmapData
          );
        } else {
          this.gl.texSubImage3D(
            this.gl.TEXTURE_2D_ARRAY,
            0,
            0,
            0,
            layer,
            // x, y, z offset
            img.w,
            img.h,
            1,
            // depth (1 layer)
            this.gl.RED,
            this.gl.UNSIGNED_BYTE,
            heap,
            img.bitmap
          );
        }
        const f = instanceCount * 5;
        this.instanceFloats[f] = img.dst_x;
        this.instanceFloats[f + 1] = img.dst_y;
        this.instanceFloats[f + 2] = img.w;
        this.instanceFloats[f + 3] = img.h;
        const b2 = instanceCount * INSTANCE_STRIDE + 16;
        this.instanceBytes[b2] = img.color >>> 24 & 255;
        this.instanceBytes[b2 + 1] = img.color >>> 16 & 255;
        this.instanceBytes[b2 + 2] = img.color >>> 8 & 255;
        this.instanceBytes[b2 + 3] = img.color & 255;
        instanceCount++;
      }
      this.gl.pixelStorei(this.gl.UNPACK_ROW_LENGTH, 0);
      if (instanceCount === 0)
        continue;
      this.gl.bindBuffer(this.gl.ARRAY_BUFFER, this.instanceBuffer);
      this.gl.bufferSubData(this.gl.ARRAY_BUFFER, 0, this.instanceBytes, 0, instanceCount * INSTANCE_STRIDE);
      this.gl.drawArraysInstanced(this.gl.TRIANGLES, 0, 6, instanceCount);
    }
    this.gl.flush();
  }
  destroy() {
    if (this.gl) {
      if (this.texArray) {
        this.gl.deleteTexture(this.texArray);
        this.texArray = null;
      }
      if (this.instanceBuffer) {
        this.gl.deleteBuffer(this.instanceBuffer);
        this.instanceBuffer = null;
      }
      if (this.vao) {
        this.gl.deleteVertexArray(this.vao);
        this.vao = null;
      }
      if (this.program) {
        this.gl.deleteProgram(this.program);
        this.program = null;
      }
      this.gl = null;
    }
  }
};

// ../../../../../../qa/lp-captions/jassub-build/jassub/dist/worker/worker.js
var constructor = /* @__PURE__ */ Symbol.for("constructor");
var ASSRenderer = class {
  _wasm;
  _subtitleColorSpace;
  _videoColorSpace;
  _malloc;
  _gpurender;
  debug = false;
  constructor(...args) {
    return this[constructor](...args).catch(console.error);
  }
  async [constructor](data, getFont, ctrl) {
    this._availableFonts = Object.fromEntries(Object.entries(data.availableFonts).map(([k, v]) => [k.trim().toLowerCase(), v]));
    this.debug = data.debug;
    this.queryFonts = data.queryFonts;
    this._getFont = getFont;
    this._defaultFont = data.defaultFont.trim().toLowerCase();
    const _fetch2 = globalThis.fetch;
    globalThis.fetch = (_) => _fetch2(data.wasmUrl);
    try {
      const testCanvas = new OffscreenCanvas(1, 1);
      if (testCanvas.getContext("webgl2")) {
        this._gpurender = new WebGL2Renderer();
      } else {
        this._gpurender = testCanvas.getContext("webgl")?.getExtension("ANGLE_instanced_arrays") ? new WebGL1Renderer() : new Canvas2DRenderer();
      }
    } catch {
      this._gpurender = new Canvas2DRenderer();
    }
    this._gpurender.setCanvas(ctrl);
    this._loadedInitialFonts = !data.fonts.length;
    const { _malloc, JASSUB } = await jassub_worker_default({ __url: data.wasmUrl, __poolSize: THREAD_COUNT > 1 ? THREAD_COUNT : 0, __out: (log) => this._log(log) });
    this._malloc = _malloc;
    this._wasm = new JASSUB(data.width, data.height, this._defaultFont);
    this._wasm.setThreads(THREAD_COUNT);
    if (!this._loadedInitialFonts)
      await this._loadInitialFonts(data.fonts);
    this._wasm.createTrackMem(data.subContent ?? await fetchtext(data.subUrl));
    this._subtitleColorSpace = LIBASS_YCBCR_MAP[this._wasm.trackColorSpace];
    if (data.libassMemoryLimit > 0 || data.libassGlyphLimit > 0) {
      this._wasm.setMemoryLimits(data.libassGlyphLimit || 0, data.libassMemoryLimit || 0);
    }
    this._checkColorSpace();
    return this;
  }
  // this passes a string of track data to libass, be it styles, events etc, which it then processes and adds to the track
  // useful for streaming subtitles
  processData(events) {
    this._wasm.processData(events);
  }
  // processes a single subtitle packet with ReadOrder, timecode and duration
  // useful for streaming subtitles from Matroska-style demuxers
  processChunk(data, timecode, duration) {
    this._wasm.processChunk(data, timecode, duration);
  }
  createEvent(event) {
    this._wasm.createEvent(event);
  }
  getEvents() {
    return this._wasm.getEvents();
  }
  setEvent(event, index) {
    this._wasm.setEvent(index, event);
  }
  removeEvent(index) {
    this._wasm.removeEvent(index);
  }
  createStyle(style) {
    this._wasm.createStyle(style);
  }
  getStyles() {
    return this._wasm.getStyles();
  }
  setStyle(style, index) {
    this._wasm.setStyle(index, style);
  }
  removeStyle(index) {
    this._wasm.removeStyle(index);
  }
  styleOverride(style) {
    this._wasm.styleOverride(style);
  }
  disableStyleOverride() {
    this._wasm.disableStyleOverride();
  }
  setTrack(content) {
    this._wasm.createTrackMem(content);
    this._subtitleColorSpace = LIBASS_YCBCR_MAP[this._wasm.trackColorSpace];
  }
  freeTrack() {
    this._wasm.removeTrack();
  }
  async setTrackByUrl(url) {
    this.setTrack(await fetchtext(url));
  }
  _checkColorSpace() {
    if (!this._subtitleColorSpace || !this._videoColorSpace)
      return;
    this._gpurender.setColorMatrix(this._subtitleColorSpace, this._videoColorSpace);
  }
  _defaultFont;
  setDefaultFont(fontName) {
    this._defaultFont = fontName.trim().toLowerCase();
    this._wasm.setDefaultFont(this._defaultFont);
  }
  async _log(log) {
    console.debug(log);
    const match = log.match(/JASSUB: fontselect:[^(]+: \(([^,]+), (\d{1,4}), \d+\)/);
    if (match && !await this._findAvailableFont(match[1].trim().toLowerCase(), WEIGHT_MAP[Math.ceil(parseInt(match[2]) / 100) - 1])) {
      await this._findAvailableFont(this._defaultFont);
    }
  }
  async addFonts(fontOrURLs) {
    if (!fontOrURLs.length)
      return false;
    const strings = [];
    const uint8s = [];
    for (const fontOrURL of fontOrURLs) {
      if (typeof fontOrURL === "string") {
        strings.push(fontOrURL);
      } else {
        uint8s.push(fontOrURL);
      }
    }
    if (uint8s.length)
      this._allocFonts(uint8s);
    return !!await Promise.allSettled(strings.map((url) => this._asyncWrite(url)));
  }
  // we don't want to run _findAvailableFont before initial fonts are loaded
  // because it could duplicate fonts
  _loadedInitialFonts = false;
  async _loadInitialFonts(fontOrURLs) {
    await this.addFonts(fontOrURLs);
    this._loadedInitialFonts = true;
    this._wasm.reloadFonts();
  }
  _getFont;
  _availableFonts = {};
  _checkedFonts = /* @__PURE__ */ new Set();
  async _findAvailableFont(fontName, weight) {
    if (!this._loadedInitialFonts)
      return;
    for (const _weight of WEIGHT_MAP) {
      if (fontName.includes(_weight)) {
        fontName = fontName.replace(_weight, "").trim();
        weight ??= _weight;
        break;
      }
    }
    weight ??= "regular";
    const key = fontName + " " + weight;
    if (this._checkedFonts.has(key))
      return;
    this._checkedFonts.add(key);
    try {
      const font = this._availableFonts[key] ?? this._availableFonts[fontName] ?? await this._queryLocalFont(fontName, weight) ?? await this._queryRemoteFont([key, fontName]);
      if (font)
        return await this.addFonts([font]);
    } catch (e) {
      console.warn("Error querying font", fontName, weight, e);
    }
  }
  queryFonts;
  async _queryLocalFont(fontName, weight) {
    if (!this.queryFonts)
      return;
    return await this._getFont(fontName, weight);
  }
  async _queryRemoteFont(postscriptNames) {
    if (this.queryFonts !== "localandremote")
      return;
    const fontData = await queryRemoteFonts({ postscriptNames });
    if (!fontData.length)
      return;
    const blob = await fontData[0].blob();
    return new Uint8Array(await blob.arrayBuffer());
  }
  async _asyncWrite(font) {
    const res = await _fetch(font);
    this._allocFonts([new Uint8Array(await res.arrayBuffer())]);
  }
  _fontId = 0;
  _allocFonts(uint8s) {
    for (const uint8 of uint8s) {
      const ptr = this._malloc(uint8.byteLength);
      self.HEAPU8RAW.set(uint8, ptr);
      this._wasm.addFont("font-" + this._fontId++, ptr, uint8.byteLength);
    }
    this._wasm.reloadFonts();
  }
  _resizeCanvas(width, height, videoWidth, videoHeight) {
    this._wasm.resizeCanvas(width, height, videoWidth, videoHeight);
    this._gpurender.resizeCanvas(width, height);
  }
  async [finalizer]() {
    this._wasm.quitLibrary();
    this._gpurender.destroy();
    this._wasm = null;
    this._gpurender = null;
    this._availableFonts = {};
  }
  _draw(time, repaint = false) {
    const images = this._wasm.rawRender(time, Number(repaint));
    if (!images)
      return;
    this._gpurender.render(images, self.HEAPU8RAW);
  }
  // single-frame readback for tests
  _drawCapture(time) {
    const images = this._wasm.rawRender(time, 1) ?? [];
    this._gpurender.render(images, self.HEAPU8RAW);
    const canvas = this._gpurender.canvas;
    if (!canvas)
      return;
    const { width, height } = canvas;
    const surface = new OffscreenCanvas(width, height);
    const ctx = surface.getContext("2d");
    if (!ctx)
      return;
    ctx.drawImage(canvas, 0, 0);
    return { data: ctx.getImageData(0, 0, width, height).data, width, height };
  }
  _setColorSpace(videoColorSpace) {
    if (videoColorSpace === "RGB")
      return;
    this._videoColorSpace = videoColorSpace;
    this._checkColorSpace();
  }
};
if (self.name === "jassub-worker") {
  expose2(ASSRenderer);
}
export {
  ASSRenderer
};

"use strict";
var __create = Object.create;
var __defProp = Object.defineProperty;
var __getOwnPropDesc = Object.getOwnPropertyDescriptor;
var __getOwnPropNames = Object.getOwnPropertyNames;
var __getProtoOf = Object.getPrototypeOf;
var __hasOwnProp = Object.prototype.hasOwnProperty;
var __esm = (fn, res) => function __init() {
  return fn && (res = (0, fn[__getOwnPropNames(fn)[0]])(fn = 0)), res;
};
var __commonJS = (cb, mod) => function __require() {
  return mod || (0, cb[__getOwnPropNames(cb)[0]])((mod = { exports: {} }).exports, mod), mod.exports;
};
var __export = (target, all) => {
  for (var name in all)
    __defProp(target, name, { get: all[name], enumerable: true });
};
var __copyProps = (to, from, except, desc) => {
  if (from && typeof from === "object" || typeof from === "function") {
    for (let key of __getOwnPropNames(from))
      if (!__hasOwnProp.call(to, key) && key !== except)
        __defProp(to, key, { get: () => from[key], enumerable: !(desc = __getOwnPropDesc(from, key)) || desc.enumerable });
  }
  return to;
};
var __toESM = (mod, isNodeMode, target) => (target = mod != null ? __create(__getProtoOf(mod)) : {}, __copyProps(
  // If the importer is in node compatibility mode or this is not an ESM
  // file that has been converted to a CommonJS file using a Babel-
  // compatible transform (i.e. "__esModule" has not been set), then set
  // "default" to the CommonJS "module.exports" for node compatibility.
  isNodeMode || !mod || !mod.__esModule ? __defProp(target, "default", { value: mod, enumerable: true }) : target,
  mod
));
var __toCommonJS = (mod) => __copyProps(__defProp({}, "__esModule", { value: true }), mod);

// node_modules/tslib/tslib.es6.mjs
var tslib_es6_exports = {};
__export(tslib_es6_exports, {
  __addDisposableResource: () => __addDisposableResource,
  __assign: () => __assign,
  __asyncDelegator: () => __asyncDelegator,
  __asyncGenerator: () => __asyncGenerator,
  __asyncValues: () => __asyncValues,
  __await: () => __await,
  __awaiter: () => __awaiter,
  __classPrivateFieldGet: () => __classPrivateFieldGet,
  __classPrivateFieldIn: () => __classPrivateFieldIn,
  __classPrivateFieldSet: () => __classPrivateFieldSet,
  __createBinding: () => __createBinding,
  __decorate: () => __decorate,
  __disposeResources: () => __disposeResources,
  __esDecorate: () => __esDecorate,
  __exportStar: () => __exportStar,
  __extends: () => __extends,
  __generator: () => __generator,
  __importDefault: () => __importDefault,
  __importStar: () => __importStar,
  __makeTemplateObject: () => __makeTemplateObject,
  __metadata: () => __metadata,
  __param: () => __param,
  __propKey: () => __propKey,
  __read: () => __read,
  __rest: () => __rest,
  __rewriteRelativeImportExtension: () => __rewriteRelativeImportExtension,
  __runInitializers: () => __runInitializers,
  __setFunctionName: () => __setFunctionName,
  __spread: () => __spread,
  __spreadArray: () => __spreadArray,
  __spreadArrays: () => __spreadArrays,
  __values: () => __values,
  default: () => tslib_es6_default
});
function __extends(d, b) {
  if (typeof b !== "function" && b !== null)
    throw new TypeError("Class extends value " + String(b) + " is not a constructor or null");
  extendStatics(d, b);
  function __() {
    this.constructor = d;
  }
  d.prototype = b === null ? Object.create(b) : (__.prototype = b.prototype, new __());
}
function __rest(s, e) {
  var t = {};
  for (var p in s) if (Object.prototype.hasOwnProperty.call(s, p) && e.indexOf(p) < 0)
    t[p] = s[p];
  if (s != null && typeof Object.getOwnPropertySymbols === "function")
    for (var i = 0, p = Object.getOwnPropertySymbols(s); i < p.length; i++) {
      if (e.indexOf(p[i]) < 0 && Object.prototype.propertyIsEnumerable.call(s, p[i]))
        t[p[i]] = s[p[i]];
    }
  return t;
}
function __decorate(decorators, target, key, desc) {
  var c = arguments.length, r = c < 3 ? target : desc === null ? desc = Object.getOwnPropertyDescriptor(target, key) : desc, d;
  if (typeof Reflect === "object" && typeof Reflect.decorate === "function") r = Reflect.decorate(decorators, target, key, desc);
  else for (var i = decorators.length - 1; i >= 0; i--) if (d = decorators[i]) r = (c < 3 ? d(r) : c > 3 ? d(target, key, r) : d(target, key)) || r;
  return c > 3 && r && Object.defineProperty(target, key, r), r;
}
function __param(paramIndex, decorator) {
  return function(target, key) {
    decorator(target, key, paramIndex);
  };
}
function __esDecorate(ctor, descriptorIn, decorators, contextIn, initializers, extraInitializers) {
  function accept(f) {
    if (f !== void 0 && typeof f !== "function") throw new TypeError("Function expected");
    return f;
  }
  var kind = contextIn.kind, key = kind === "getter" ? "get" : kind === "setter" ? "set" : "value";
  var target = !descriptorIn && ctor ? contextIn["static"] ? ctor : ctor.prototype : null;
  var descriptor = descriptorIn || (target ? Object.getOwnPropertyDescriptor(target, contextIn.name) : {});
  var _, done = false;
  for (var i = decorators.length - 1; i >= 0; i--) {
    var context = {};
    for (var p in contextIn) context[p] = p === "access" ? {} : contextIn[p];
    for (var p in contextIn.access) context.access[p] = contextIn.access[p];
    context.addInitializer = function(f) {
      if (done) throw new TypeError("Cannot add initializers after decoration has completed");
      extraInitializers.push(accept(f || null));
    };
    var result = (0, decorators[i])(kind === "accessor" ? { get: descriptor.get, set: descriptor.set } : descriptor[key], context);
    if (kind === "accessor") {
      if (result === void 0) continue;
      if (result === null || typeof result !== "object") throw new TypeError("Object expected");
      if (_ = accept(result.get)) descriptor.get = _;
      if (_ = accept(result.set)) descriptor.set = _;
      if (_ = accept(result.init)) initializers.unshift(_);
    } else if (_ = accept(result)) {
      if (kind === "field") initializers.unshift(_);
      else descriptor[key] = _;
    }
  }
  if (target) Object.defineProperty(target, contextIn.name, descriptor);
  done = true;
}
function __runInitializers(thisArg, initializers, value) {
  var useValue = arguments.length > 2;
  for (var i = 0; i < initializers.length; i++) {
    value = useValue ? initializers[i].call(thisArg, value) : initializers[i].call(thisArg);
  }
  return useValue ? value : void 0;
}
function __propKey(x) {
  return typeof x === "symbol" ? x : "".concat(x);
}
function __setFunctionName(f, name, prefix) {
  if (typeof name === "symbol") name = name.description ? "[".concat(name.description, "]") : "";
  return Object.defineProperty(f, "name", { configurable: true, value: prefix ? "".concat(prefix, " ", name) : name });
}
function __metadata(metadataKey, metadataValue) {
  if (typeof Reflect === "object" && typeof Reflect.metadata === "function") return Reflect.metadata(metadataKey, metadataValue);
}
function __awaiter(thisArg, _arguments, P, generator) {
  function adopt(value) {
    return value instanceof P ? value : new P(function(resolve) {
      resolve(value);
    });
  }
  return new (P || (P = Promise))(function(resolve, reject) {
    function fulfilled(value) {
      try {
        step(generator.next(value));
      } catch (e) {
        reject(e);
      }
    }
    function rejected(value) {
      try {
        step(generator["throw"](value));
      } catch (e) {
        reject(e);
      }
    }
    function step(result) {
      result.done ? resolve(result.value) : adopt(result.value).then(fulfilled, rejected);
    }
    step((generator = generator.apply(thisArg, _arguments || [])).next());
  });
}
function __generator(thisArg, body) {
  var _ = { label: 0, sent: function() {
    if (t[0] & 1) throw t[1];
    return t[1];
  }, trys: [], ops: [] }, f, y, t, g = Object.create((typeof Iterator === "function" ? Iterator : Object).prototype);
  return g.next = verb(0), g["throw"] = verb(1), g["return"] = verb(2), typeof Symbol === "function" && (g[Symbol.iterator] = function() {
    return this;
  }), g;
  function verb(n) {
    return function(v) {
      return step([n, v]);
    };
  }
  function step(op) {
    if (f) throw new TypeError("Generator is already executing.");
    while (g && (g = 0, op[0] && (_ = 0)), _) try {
      if (f = 1, y && (t = op[0] & 2 ? y["return"] : op[0] ? y["throw"] || ((t = y["return"]) && t.call(y), 0) : y.next) && !(t = t.call(y, op[1])).done) return t;
      if (y = 0, t) op = [op[0] & 2, t.value];
      switch (op[0]) {
        case 0:
        case 1:
          t = op;
          break;
        case 4:
          _.label++;
          return { value: op[1], done: false };
        case 5:
          _.label++;
          y = op[1];
          op = [0];
          continue;
        case 7:
          op = _.ops.pop();
          _.trys.pop();
          continue;
        default:
          if (!(t = _.trys, t = t.length > 0 && t[t.length - 1]) && (op[0] === 6 || op[0] === 2)) {
            _ = 0;
            continue;
          }
          if (op[0] === 3 && (!t || op[1] > t[0] && op[1] < t[3])) {
            _.label = op[1];
            break;
          }
          if (op[0] === 6 && _.label < t[1]) {
            _.label = t[1];
            t = op;
            break;
          }
          if (t && _.label < t[2]) {
            _.label = t[2];
            _.ops.push(op);
            break;
          }
          if (t[2]) _.ops.pop();
          _.trys.pop();
          continue;
      }
      op = body.call(thisArg, _);
    } catch (e) {
      op = [6, e];
      y = 0;
    } finally {
      f = t = 0;
    }
    if (op[0] & 5) throw op[1];
    return { value: op[0] ? op[1] : void 0, done: true };
  }
}
function __exportStar(m, o) {
  for (var p in m) if (p !== "default" && !Object.prototype.hasOwnProperty.call(o, p)) __createBinding(o, m, p);
}
function __values(o) {
  var s = typeof Symbol === "function" && Symbol.iterator, m = s && o[s], i = 0;
  if (m) return m.call(o);
  if (o && typeof o.length === "number") return {
    next: function() {
      if (o && i >= o.length) o = void 0;
      return { value: o && o[i++], done: !o };
    }
  };
  throw new TypeError(s ? "Object is not iterable." : "Symbol.iterator is not defined.");
}
function __read(o, n) {
  var m = typeof Symbol === "function" && o[Symbol.iterator];
  if (!m) return o;
  var i = m.call(o), r, ar = [], e;
  try {
    while ((n === void 0 || n-- > 0) && !(r = i.next()).done) ar.push(r.value);
  } catch (error) {
    e = { error };
  } finally {
    try {
      if (r && !r.done && (m = i["return"])) m.call(i);
    } finally {
      if (e) throw e.error;
    }
  }
  return ar;
}
function __spread() {
  for (var ar = [], i = 0; i < arguments.length; i++)
    ar = ar.concat(__read(arguments[i]));
  return ar;
}
function __spreadArrays() {
  for (var s = 0, i = 0, il = arguments.length; i < il; i++) s += arguments[i].length;
  for (var r = Array(s), k = 0, i = 0; i < il; i++)
    for (var a = arguments[i], j = 0, jl = a.length; j < jl; j++, k++)
      r[k] = a[j];
  return r;
}
function __spreadArray(to, from, pack) {
  if (pack || arguments.length === 2) for (var i = 0, l = from.length, ar; i < l; i++) {
    if (ar || !(i in from)) {
      if (!ar) ar = Array.prototype.slice.call(from, 0, i);
      ar[i] = from[i];
    }
  }
  return to.concat(ar || Array.prototype.slice.call(from));
}
function __await(v) {
  return this instanceof __await ? (this.v = v, this) : new __await(v);
}
function __asyncGenerator(thisArg, _arguments, generator) {
  if (!Symbol.asyncIterator) throw new TypeError("Symbol.asyncIterator is not defined.");
  var g = generator.apply(thisArg, _arguments || []), i, q = [];
  return i = Object.create((typeof AsyncIterator === "function" ? AsyncIterator : Object).prototype), verb("next"), verb("throw"), verb("return", awaitReturn), i[Symbol.asyncIterator] = function() {
    return this;
  }, i;
  function awaitReturn(f) {
    return function(v) {
      return Promise.resolve(v).then(f, reject);
    };
  }
  function verb(n, f) {
    if (g[n]) {
      i[n] = function(v) {
        return new Promise(function(a, b) {
          q.push([n, v, a, b]) > 1 || resume(n, v);
        });
      };
      if (f) i[n] = f(i[n]);
    }
  }
  function resume(n, v) {
    try {
      step(g[n](v));
    } catch (e) {
      settle(q[0][3], e);
    }
  }
  function step(r) {
    r.value instanceof __await ? Promise.resolve(r.value.v).then(fulfill, reject) : settle(q[0][2], r);
  }
  function fulfill(value) {
    resume("next", value);
  }
  function reject(value) {
    resume("throw", value);
  }
  function settle(f, v) {
    if (f(v), q.shift(), q.length) resume(q[0][0], q[0][1]);
  }
}
function __asyncDelegator(o) {
  var i, p;
  return i = {}, verb("next"), verb("throw", function(e) {
    throw e;
  }), verb("return"), i[Symbol.iterator] = function() {
    return this;
  }, i;
  function verb(n, f) {
    i[n] = o[n] ? function(v) {
      return (p = !p) ? { value: __await(o[n](v)), done: false } : f ? f(v) : v;
    } : f;
  }
}
function __asyncValues(o) {
  if (!Symbol.asyncIterator) throw new TypeError("Symbol.asyncIterator is not defined.");
  var m = o[Symbol.asyncIterator], i;
  return m ? m.call(o) : (o = typeof __values === "function" ? __values(o) : o[Symbol.iterator](), i = {}, verb("next"), verb("throw"), verb("return"), i[Symbol.asyncIterator] = function() {
    return this;
  }, i);
  function verb(n) {
    i[n] = o[n] && function(v) {
      return new Promise(function(resolve, reject) {
        v = o[n](v), settle(resolve, reject, v.done, v.value);
      });
    };
  }
  function settle(resolve, reject, d, v) {
    Promise.resolve(v).then(function(v2) {
      resolve({ value: v2, done: d });
    }, reject);
  }
}
function __makeTemplateObject(cooked, raw) {
  if (Object.defineProperty) {
    Object.defineProperty(cooked, "raw", { value: raw });
  } else {
    cooked.raw = raw;
  }
  return cooked;
}
function __importStar(mod) {
  if (mod && mod.__esModule) return mod;
  var result = {};
  if (mod != null) {
    for (var k = ownKeys(mod), i = 0; i < k.length; i++) if (k[i] !== "default") __createBinding(result, mod, k[i]);
  }
  __setModuleDefault(result, mod);
  return result;
}
function __importDefault(mod) {
  return mod && mod.__esModule ? mod : { default: mod };
}
function __classPrivateFieldGet(receiver, state, kind, f) {
  if (kind === "a" && !f) throw new TypeError("Private accessor was defined without a getter");
  if (typeof state === "function" ? receiver !== state || !f : !state.has(receiver)) throw new TypeError("Cannot read private member from an object whose class did not declare it");
  return kind === "m" ? f : kind === "a" ? f.call(receiver) : f ? f.value : state.get(receiver);
}
function __classPrivateFieldSet(receiver, state, value, kind, f) {
  if (kind === "m") throw new TypeError("Private method is not writable");
  if (kind === "a" && !f) throw new TypeError("Private accessor was defined without a setter");
  if (typeof state === "function" ? receiver !== state || !f : !state.has(receiver)) throw new TypeError("Cannot write private member to an object whose class did not declare it");
  return kind === "a" ? f.call(receiver, value) : f ? f.value = value : state.set(receiver, value), value;
}
function __classPrivateFieldIn(state, receiver) {
  if (receiver === null || typeof receiver !== "object" && typeof receiver !== "function") throw new TypeError("Cannot use 'in' operator on non-object");
  return typeof state === "function" ? receiver === state : state.has(receiver);
}
function __addDisposableResource(env, value, async) {
  if (value !== null && value !== void 0) {
    if (typeof value !== "object" && typeof value !== "function") throw new TypeError("Object expected.");
    var dispose, inner;
    if (async) {
      if (!Symbol.asyncDispose) throw new TypeError("Symbol.asyncDispose is not defined.");
      dispose = value[Symbol.asyncDispose];
    }
    if (dispose === void 0) {
      if (!Symbol.dispose) throw new TypeError("Symbol.dispose is not defined.");
      dispose = value[Symbol.dispose];
      if (async) inner = dispose;
    }
    if (typeof dispose !== "function") throw new TypeError("Object not disposable.");
    if (inner) dispose = function() {
      try {
        inner.call(this);
      } catch (e) {
        return Promise.reject(e);
      }
    };
    env.stack.push({ value, dispose, async });
  } else if (async) {
    env.stack.push({ async: true });
  }
  return value;
}
function __disposeResources(env) {
  function fail(e) {
    env.error = env.hasError ? new _SuppressedError(e, env.error, "An error was suppressed during disposal.") : e;
    env.hasError = true;
  }
  var r, s = 0;
  function next() {
    while (r = env.stack.pop()) {
      try {
        if (!r.async && s === 1) return s = 0, env.stack.push(r), Promise.resolve().then(next);
        if (r.dispose) {
          var result = r.dispose.call(r.value);
          if (r.async) return s |= 2, Promise.resolve(result).then(next, function(e) {
            fail(e);
            return next();
          });
        } else s |= 1;
      } catch (e) {
        fail(e);
      }
    }
    if (s === 1) return env.hasError ? Promise.reject(env.error) : Promise.resolve();
    if (env.hasError) throw env.error;
  }
  return next();
}
function __rewriteRelativeImportExtension(path, preserveJsx) {
  if (typeof path === "string" && /^\.\.?\//.test(path)) {
    return path.replace(/\.(tsx)$|((?:\.d)?)((?:\.[^./]+?)?)\.([cm]?)ts$/i, function(m, tsx, d, ext, cm) {
      return tsx ? preserveJsx ? ".jsx" : ".js" : d && (!ext || !cm) ? m : d + ext + "." + cm.toLowerCase() + "js";
    });
  }
  return path;
}
var extendStatics, __assign, __createBinding, __setModuleDefault, ownKeys, _SuppressedError, tslib_es6_default;
var init_tslib_es6 = __esm({
  "node_modules/tslib/tslib.es6.mjs"() {
    "use strict";
    extendStatics = function(d, b) {
      extendStatics = Object.setPrototypeOf || { __proto__: [] } instanceof Array && function(d2, b2) {
        d2.__proto__ = b2;
      } || function(d2, b2) {
        for (var p in b2) if (Object.prototype.hasOwnProperty.call(b2, p)) d2[p] = b2[p];
      };
      return extendStatics(d, b);
    };
    __assign = function() {
      __assign = Object.assign || function __assign2(t) {
        for (var s, i = 1, n = arguments.length; i < n; i++) {
          s = arguments[i];
          for (var p in s) if (Object.prototype.hasOwnProperty.call(s, p)) t[p] = s[p];
        }
        return t;
      };
      return __assign.apply(this, arguments);
    };
    __createBinding = Object.create ? (function(o, m, k, k2) {
      if (k2 === void 0) k2 = k;
      var desc = Object.getOwnPropertyDescriptor(m, k);
      if (!desc || ("get" in desc ? !m.__esModule : desc.writable || desc.configurable)) {
        desc = { enumerable: true, get: function() {
          return m[k];
        } };
      }
      Object.defineProperty(o, k2, desc);
    }) : (function(o, m, k, k2) {
      if (k2 === void 0) k2 = k;
      o[k2] = m[k];
    });
    __setModuleDefault = Object.create ? (function(o, v) {
      Object.defineProperty(o, "default", { enumerable: true, value: v });
    }) : function(o, v) {
      o["default"] = v;
    };
    ownKeys = function(o) {
      ownKeys = Object.getOwnPropertyNames || function(o2) {
        var ar = [];
        for (var k in o2) if (Object.prototype.hasOwnProperty.call(o2, k)) ar[ar.length] = k;
        return ar;
      };
      return ownKeys(o);
    };
    _SuppressedError = typeof SuppressedError === "function" ? SuppressedError : function(error, suppressed, message) {
      var e = new Error(message);
      return e.name = "SuppressedError", e.error = error, e.suppressed = suppressed, e;
    };
    tslib_es6_default = {
      __extends,
      __assign,
      __rest,
      __decorate,
      __param,
      __esDecorate,
      __runInitializers,
      __propKey,
      __setFunctionName,
      __metadata,
      __awaiter,
      __generator,
      __createBinding,
      __exportStar,
      __values,
      __read,
      __spread,
      __spreadArrays,
      __spreadArray,
      __await,
      __asyncGenerator,
      __asyncDelegator,
      __asyncValues,
      __makeTemplateObject,
      __importStar,
      __importDefault,
      __classPrivateFieldGet,
      __classPrivateFieldSet,
      __classPrivateFieldIn,
      __addDisposableResource,
      __disposeResources,
      __rewriteRelativeImportExtension
    };
  }
});

// node_modules/apache-arrow/util/utf8.js
var require_utf8 = __commonJS({
  "node_modules/apache-arrow/util/utf8.js"(exports2) {
    "use strict";
    Object.defineProperty(exports2, "__esModule", { value: true });
    exports2.encodeUtf8 = exports2.decodeUtf8 = void 0;
    var decoder = new TextDecoder("utf-8");
    var decodeUtf8 = (buffer) => decoder.decode(buffer);
    exports2.decodeUtf8 = decodeUtf8;
    var encoder = new TextEncoder();
    var encodeUtf8 = (value) => encoder.encode(value);
    exports2.encodeUtf8 = encodeUtf8;
  }
});

// node_modules/apache-arrow/util/compat.js
var require_compat = __commonJS({
  "node_modules/apache-arrow/util/compat.js"(exports2) {
    "use strict";
    Object.defineProperty(exports2, "__esModule", { value: true });
    exports2.isFlatbuffersByteBuffer = exports2.isReadableNodeStream = exports2.isWritableNodeStream = exports2.isReadableDOMStream = exports2.isWritableDOMStream = exports2.isFetchResponse = exports2.isFSReadStream = exports2.isFileHandle = exports2.isUnderlyingSink = exports2.isIteratorResult = exports2.isArrayLike = exports2.isArrowJSON = exports2.isAsyncIterable = exports2.isIterable = exports2.isObservable = exports2.isPromise = exports2.isObject = void 0;
    var isNumber = (x) => typeof x === "number";
    var isBoolean = (x) => typeof x === "boolean";
    var isFunction = (x) => typeof x === "function";
    var isObject = (x) => x != null && Object(x) === x;
    exports2.isObject = isObject;
    var isPromise = (x) => {
      return (0, exports2.isObject)(x) && isFunction(x.then);
    };
    exports2.isPromise = isPromise;
    var isObservable = (x) => {
      return (0, exports2.isObject)(x) && isFunction(x.subscribe);
    };
    exports2.isObservable = isObservable;
    var isIterable = (x) => {
      return (0, exports2.isObject)(x) && isFunction(x[Symbol.iterator]);
    };
    exports2.isIterable = isIterable;
    var isAsyncIterable = (x) => {
      return (0, exports2.isObject)(x) && isFunction(x[Symbol.asyncIterator]);
    };
    exports2.isAsyncIterable = isAsyncIterable;
    var isArrowJSON = (x) => {
      return (0, exports2.isObject)(x) && (0, exports2.isObject)(x["schema"]);
    };
    exports2.isArrowJSON = isArrowJSON;
    var isArrayLike = (x) => {
      return (0, exports2.isObject)(x) && isNumber(x["length"]);
    };
    exports2.isArrayLike = isArrayLike;
    var isIteratorResult = (x) => {
      return (0, exports2.isObject)(x) && "done" in x && "value" in x;
    };
    exports2.isIteratorResult = isIteratorResult;
    var isUnderlyingSink = (x) => {
      return (0, exports2.isObject)(x) && isFunction(x["abort"]) && isFunction(x["close"]) && isFunction(x["start"]) && isFunction(x["write"]);
    };
    exports2.isUnderlyingSink = isUnderlyingSink;
    var isFileHandle = (x) => {
      return (0, exports2.isObject)(x) && isFunction(x["stat"]) && isNumber(x["fd"]);
    };
    exports2.isFileHandle = isFileHandle;
    var isFSReadStream = (x) => {
      return (0, exports2.isReadableNodeStream)(x) && isNumber(x["bytesRead"]);
    };
    exports2.isFSReadStream = isFSReadStream;
    var isFetchResponse = (x) => {
      return (0, exports2.isObject)(x) && (0, exports2.isReadableDOMStream)(x["body"]);
    };
    exports2.isFetchResponse = isFetchResponse;
    var isReadableInterop = (x) => "_getDOMStream" in x && "_getNodeStream" in x;
    var isWritableDOMStream = (x) => {
      return (0, exports2.isObject)(x) && isFunction(x["abort"]) && isFunction(x["getWriter"]) && !isReadableInterop(x);
    };
    exports2.isWritableDOMStream = isWritableDOMStream;
    var isReadableDOMStream = (x) => {
      return (0, exports2.isObject)(x) && isFunction(x["cancel"]) && isFunction(x["getReader"]) && !isReadableInterop(x);
    };
    exports2.isReadableDOMStream = isReadableDOMStream;
    var isWritableNodeStream = (x) => {
      return (0, exports2.isObject)(x) && isFunction(x["end"]) && isFunction(x["write"]) && isBoolean(x["writable"]) && !isReadableInterop(x);
    };
    exports2.isWritableNodeStream = isWritableNodeStream;
    var isReadableNodeStream = (x) => {
      return (0, exports2.isObject)(x) && isFunction(x["read"]) && isFunction(x["pipe"]) && isBoolean(x["readable"]) && !isReadableInterop(x);
    };
    exports2.isReadableNodeStream = isReadableNodeStream;
    var isFlatbuffersByteBuffer = (x) => {
      return (0, exports2.isObject)(x) && isFunction(x["clear"]) && isFunction(x["bytes"]) && isFunction(x["position"]) && isFunction(x["setPosition"]) && isFunction(x["capacity"]) && isFunction(x["getBufferIdentifier"]) && isFunction(x["createLong"]);
    };
    exports2.isFlatbuffersByteBuffer = isFlatbuffersByteBuffer;
  }
});

// node_modules/apache-arrow/util/buffer.js
var require_buffer = __commonJS({
  "node_modules/apache-arrow/util/buffer.js"(exports2) {
    "use strict";
    Object.defineProperty(exports2, "__esModule", { value: true });
    exports2.compareArrayLike = exports2.rebaseValueOffsets = exports2.toUint8ClampedArrayAsyncIterator = exports2.toFloat64ArrayAsyncIterator = exports2.toFloat32ArrayAsyncIterator = exports2.toUint32ArrayAsyncIterator = exports2.toUint16ArrayAsyncIterator = exports2.toUint8ArrayAsyncIterator = exports2.toInt32ArrayAsyncIterator = exports2.toInt16ArrayAsyncIterator = exports2.toInt8ArrayAsyncIterator = exports2.toArrayBufferViewAsyncIterator = exports2.toUint8ClampedArrayIterator = exports2.toFloat64ArrayIterator = exports2.toFloat32ArrayIterator = exports2.toUint32ArrayIterator = exports2.toUint16ArrayIterator = exports2.toUint8ArrayIterator = exports2.toInt32ArrayIterator = exports2.toInt16ArrayIterator = exports2.toInt8ArrayIterator = exports2.toArrayBufferViewIterator = exports2.toUint8ClampedArray = exports2.toFloat64Array = exports2.toFloat32Array = exports2.toBigUint64Array = exports2.toUint32Array = exports2.toUint16Array = exports2.toUint8Array = exports2.toBigInt64Array = exports2.toInt32Array = exports2.toInt16Array = exports2.toInt8Array = exports2.toArrayBufferView = exports2.joinUint8Arrays = exports2.memcpy = void 0;
    var tslib_1 = (init_tslib_es6(), __toCommonJS(tslib_es6_exports));
    var utf8_js_1 = require_utf8();
    var compat_js_1 = require_compat();
    var SharedArrayBuf = typeof SharedArrayBuffer !== "undefined" ? SharedArrayBuffer : ArrayBuffer;
    function collapseContiguousByteRanges(chunks) {
      const result = chunks[0] ? [chunks[0]] : [];
      let xOffset, yOffset, xLen, yLen;
      for (let x, y, i = 0, j = 0, n = chunks.length; ++i < n; ) {
        x = result[j];
        y = chunks[i];
        if (!x || !y || x.buffer !== y.buffer || y.byteOffset < x.byteOffset) {
          y && (result[++j] = y);
          continue;
        }
        ({ byteOffset: xOffset, byteLength: xLen } = x);
        ({ byteOffset: yOffset, byteLength: yLen } = y);
        if (xOffset + xLen < yOffset || yOffset + yLen < xOffset) {
          y && (result[++j] = y);
          continue;
        }
        result[j] = new Uint8Array(x.buffer, xOffset, yOffset - xOffset + yLen);
      }
      return result;
    }
    function memcpy(target, source, targetByteOffset = 0, sourceByteLength = source.byteLength) {
      const targetByteLength = target.byteLength;
      const dst = new Uint8Array(target.buffer, target.byteOffset, targetByteLength);
      const src = new Uint8Array(source.buffer, source.byteOffset, Math.min(sourceByteLength, targetByteLength));
      dst.set(src, targetByteOffset);
      return target;
    }
    exports2.memcpy = memcpy;
    function joinUint8Arrays(chunks, size) {
      const result = collapseContiguousByteRanges(chunks);
      const byteLength = result.reduce((x, b) => x + b.byteLength, 0);
      let source, sliced, buffer;
      let offset = 0, index = -1;
      const length = Math.min(size || Number.POSITIVE_INFINITY, byteLength);
      for (const n = result.length; ++index < n; ) {
        source = result[index];
        sliced = source.subarray(0, Math.min(source.length, length - offset));
        if (length <= offset + sliced.length) {
          if (sliced.length < source.length) {
            result[index] = source.subarray(sliced.length);
          } else if (sliced.length === source.length) {
            index++;
          }
          buffer ? memcpy(buffer, sliced, offset) : buffer = sliced;
          break;
        }
        memcpy(buffer || (buffer = new Uint8Array(length)), sliced, offset);
        offset += sliced.length;
      }
      return [buffer || new Uint8Array(0), result.slice(index), byteLength - (buffer ? buffer.byteLength : 0)];
    }
    exports2.joinUint8Arrays = joinUint8Arrays;
    function toArrayBufferView(ArrayBufferViewCtor, input) {
      let value = (0, compat_js_1.isIteratorResult)(input) ? input.value : input;
      if (value instanceof ArrayBufferViewCtor) {
        if (ArrayBufferViewCtor === Uint8Array) {
          return new ArrayBufferViewCtor(value.buffer, value.byteOffset, value.byteLength);
        }
        return value;
      }
      if (!value) {
        return new ArrayBufferViewCtor(0);
      }
      if (typeof value === "string") {
        value = (0, utf8_js_1.encodeUtf8)(value);
      }
      if (value instanceof ArrayBuffer) {
        return new ArrayBufferViewCtor(value);
      }
      if (value instanceof SharedArrayBuf) {
        return new ArrayBufferViewCtor(value);
      }
      if ((0, compat_js_1.isFlatbuffersByteBuffer)(value)) {
        return toArrayBufferView(ArrayBufferViewCtor, value.bytes());
      }
      return !ArrayBuffer.isView(value) ? ArrayBufferViewCtor.from(value) : value.byteLength <= 0 ? new ArrayBufferViewCtor(0) : new ArrayBufferViewCtor(value.buffer, value.byteOffset, value.byteLength / ArrayBufferViewCtor.BYTES_PER_ELEMENT);
    }
    exports2.toArrayBufferView = toArrayBufferView;
    var toInt8Array = (input) => toArrayBufferView(Int8Array, input);
    exports2.toInt8Array = toInt8Array;
    var toInt16Array = (input) => toArrayBufferView(Int16Array, input);
    exports2.toInt16Array = toInt16Array;
    var toInt32Array = (input) => toArrayBufferView(Int32Array, input);
    exports2.toInt32Array = toInt32Array;
    var toBigInt64Array = (input) => toArrayBufferView(BigInt64Array, input);
    exports2.toBigInt64Array = toBigInt64Array;
    var toUint8Array = (input) => toArrayBufferView(Uint8Array, input);
    exports2.toUint8Array = toUint8Array;
    var toUint16Array = (input) => toArrayBufferView(Uint16Array, input);
    exports2.toUint16Array = toUint16Array;
    var toUint32Array = (input) => toArrayBufferView(Uint32Array, input);
    exports2.toUint32Array = toUint32Array;
    var toBigUint64Array = (input) => toArrayBufferView(BigUint64Array, input);
    exports2.toBigUint64Array = toBigUint64Array;
    var toFloat32Array = (input) => toArrayBufferView(Float32Array, input);
    exports2.toFloat32Array = toFloat32Array;
    var toFloat64Array = (input) => toArrayBufferView(Float64Array, input);
    exports2.toFloat64Array = toFloat64Array;
    var toUint8ClampedArray = (input) => toArrayBufferView(Uint8ClampedArray, input);
    exports2.toUint8ClampedArray = toUint8ClampedArray;
    var pump = (iterator) => {
      iterator.next();
      return iterator;
    };
    function* toArrayBufferViewIterator(ArrayCtor, source) {
      const wrap = function* (x) {
        yield x;
      };
      const buffers = typeof source === "string" ? wrap(source) : ArrayBuffer.isView(source) ? wrap(source) : source instanceof ArrayBuffer ? wrap(source) : source instanceof SharedArrayBuf ? wrap(source) : !(0, compat_js_1.isIterable)(source) ? wrap(source) : source;
      yield* pump((function* (it) {
        let r = null;
        do {
          r = it.next(yield toArrayBufferView(ArrayCtor, r));
        } while (!r.done);
      })(buffers[Symbol.iterator]()));
      return new ArrayCtor();
    }
    exports2.toArrayBufferViewIterator = toArrayBufferViewIterator;
    var toInt8ArrayIterator = (input) => toArrayBufferViewIterator(Int8Array, input);
    exports2.toInt8ArrayIterator = toInt8ArrayIterator;
    var toInt16ArrayIterator = (input) => toArrayBufferViewIterator(Int16Array, input);
    exports2.toInt16ArrayIterator = toInt16ArrayIterator;
    var toInt32ArrayIterator = (input) => toArrayBufferViewIterator(Int32Array, input);
    exports2.toInt32ArrayIterator = toInt32ArrayIterator;
    var toUint8ArrayIterator = (input) => toArrayBufferViewIterator(Uint8Array, input);
    exports2.toUint8ArrayIterator = toUint8ArrayIterator;
    var toUint16ArrayIterator = (input) => toArrayBufferViewIterator(Uint16Array, input);
    exports2.toUint16ArrayIterator = toUint16ArrayIterator;
    var toUint32ArrayIterator = (input) => toArrayBufferViewIterator(Uint32Array, input);
    exports2.toUint32ArrayIterator = toUint32ArrayIterator;
    var toFloat32ArrayIterator = (input) => toArrayBufferViewIterator(Float32Array, input);
    exports2.toFloat32ArrayIterator = toFloat32ArrayIterator;
    var toFloat64ArrayIterator = (input) => toArrayBufferViewIterator(Float64Array, input);
    exports2.toFloat64ArrayIterator = toFloat64ArrayIterator;
    var toUint8ClampedArrayIterator = (input) => toArrayBufferViewIterator(Uint8ClampedArray, input);
    exports2.toUint8ClampedArrayIterator = toUint8ClampedArrayIterator;
    function toArrayBufferViewAsyncIterator(ArrayCtor, source) {
      return tslib_1.__asyncGenerator(this, arguments, function* toArrayBufferViewAsyncIterator_1() {
        if ((0, compat_js_1.isPromise)(source)) {
          return yield tslib_1.__await(yield tslib_1.__await(yield* tslib_1.__asyncDelegator(tslib_1.__asyncValues(toArrayBufferViewAsyncIterator(ArrayCtor, yield tslib_1.__await(source))))));
        }
        const wrap = function(x) {
          return tslib_1.__asyncGenerator(this, arguments, function* () {
            yield yield tslib_1.__await(yield tslib_1.__await(x));
          });
        };
        const emit = function(source2) {
          return tslib_1.__asyncGenerator(this, arguments, function* () {
            yield tslib_1.__await(yield* tslib_1.__asyncDelegator(tslib_1.__asyncValues(pump((function* (it) {
              let r = null;
              do {
                r = it.next(yield r === null || r === void 0 ? void 0 : r.value);
              } while (!r.done);
            })(source2[Symbol.iterator]())))));
          });
        };
        const buffers = typeof source === "string" ? wrap(source) : ArrayBuffer.isView(source) ? wrap(source) : source instanceof ArrayBuffer ? wrap(source) : source instanceof SharedArrayBuf ? wrap(source) : (0, compat_js_1.isIterable)(source) ? emit(source) : !(0, compat_js_1.isAsyncIterable)(source) ? wrap(source) : source;
        yield tslib_1.__await(
          // otherwise if AsyncIterable, use it
          yield* tslib_1.__asyncDelegator(tslib_1.__asyncValues(pump((function(it) {
            return tslib_1.__asyncGenerator(this, arguments, function* () {
              let r = null;
              do {
                r = yield tslib_1.__await(it.next(yield yield tslib_1.__await(toArrayBufferView(ArrayCtor, r))));
              } while (!r.done);
            });
          })(buffers[Symbol.asyncIterator]()))))
        );
        return yield tslib_1.__await(new ArrayCtor());
      });
    }
    exports2.toArrayBufferViewAsyncIterator = toArrayBufferViewAsyncIterator;
    var toInt8ArrayAsyncIterator = (input) => toArrayBufferViewAsyncIterator(Int8Array, input);
    exports2.toInt8ArrayAsyncIterator = toInt8ArrayAsyncIterator;
    var toInt16ArrayAsyncIterator = (input) => toArrayBufferViewAsyncIterator(Int16Array, input);
    exports2.toInt16ArrayAsyncIterator = toInt16ArrayAsyncIterator;
    var toInt32ArrayAsyncIterator = (input) => toArrayBufferViewAsyncIterator(Int32Array, input);
    exports2.toInt32ArrayAsyncIterator = toInt32ArrayAsyncIterator;
    var toUint8ArrayAsyncIterator = (input) => toArrayBufferViewAsyncIterator(Uint8Array, input);
    exports2.toUint8ArrayAsyncIterator = toUint8ArrayAsyncIterator;
    var toUint16ArrayAsyncIterator = (input) => toArrayBufferViewAsyncIterator(Uint16Array, input);
    exports2.toUint16ArrayAsyncIterator = toUint16ArrayAsyncIterator;
    var toUint32ArrayAsyncIterator = (input) => toArrayBufferViewAsyncIterator(Uint32Array, input);
    exports2.toUint32ArrayAsyncIterator = toUint32ArrayAsyncIterator;
    var toFloat32ArrayAsyncIterator = (input) => toArrayBufferViewAsyncIterator(Float32Array, input);
    exports2.toFloat32ArrayAsyncIterator = toFloat32ArrayAsyncIterator;
    var toFloat64ArrayAsyncIterator = (input) => toArrayBufferViewAsyncIterator(Float64Array, input);
    exports2.toFloat64ArrayAsyncIterator = toFloat64ArrayAsyncIterator;
    var toUint8ClampedArrayAsyncIterator = (input) => toArrayBufferViewAsyncIterator(Uint8ClampedArray, input);
    exports2.toUint8ClampedArrayAsyncIterator = toUint8ClampedArrayAsyncIterator;
    function rebaseValueOffsets(offset, length, valueOffsets) {
      if (offset !== 0) {
        valueOffsets = valueOffsets.slice(0, length);
        for (let i = -1, n = valueOffsets.length; ++i < n; ) {
          valueOffsets[i] += offset;
        }
      }
      return valueOffsets.subarray(0, length);
    }
    exports2.rebaseValueOffsets = rebaseValueOffsets;
    function compareArrayLike(a, b) {
      let i = 0;
      const n = a.length;
      if (n !== b.length) {
        return false;
      }
      if (n > 0) {
        do {
          if (a[i] !== b[i]) {
            return false;
          }
        } while (++i < n);
      }
      return true;
    }
    exports2.compareArrayLike = compareArrayLike;
  }
});

// node_modules/apache-arrow/io/adapters.js
var require_adapters = __commonJS({
  "node_modules/apache-arrow/io/adapters.js"(exports2) {
    "use strict";
    Object.defineProperty(exports2, "__esModule", { value: true });
    var tslib_1 = (init_tslib_es6(), __toCommonJS(tslib_es6_exports));
    var buffer_js_1 = require_buffer();
    exports2.default = {
      fromIterable(source) {
        return pump(fromIterable(source));
      },
      fromAsyncIterable(source) {
        return pump(fromAsyncIterable(source));
      },
      fromDOMStream(source) {
        return pump(fromDOMStream(source));
      },
      fromNodeStream(stream) {
        return pump(fromNodeStream(stream));
      },
      // @ts-ignore
      toDOMStream(source, options) {
        throw new Error(`"toDOMStream" not available in this environment`);
      },
      // @ts-ignore
      toNodeStream(source, options) {
        throw new Error(`"toNodeStream" not available in this environment`);
      }
    };
    var pump = (iterator) => {
      iterator.next();
      return iterator;
    };
    function* fromIterable(source) {
      let done, threw = false;
      let buffers = [], buffer;
      let cmd, size, bufferLength = 0;
      function byteRange() {
        if (cmd === "peek") {
          return (0, buffer_js_1.joinUint8Arrays)(buffers, size)[0];
        }
        [buffer, buffers, bufferLength] = (0, buffer_js_1.joinUint8Arrays)(buffers, size);
        return buffer;
      }
      ({ cmd, size } = (yield /* @__PURE__ */ (() => null)()) || { cmd: "read", size: 0 });
      const it = (0, buffer_js_1.toUint8ArrayIterator)(source)[Symbol.iterator]();
      try {
        do {
          ({ done, value: buffer } = Number.isNaN(size - bufferLength) ? it.next() : it.next(size - bufferLength));
          if (!done && buffer.byteLength > 0) {
            buffers.push(buffer);
            bufferLength += buffer.byteLength;
          }
          if (done || size <= bufferLength) {
            do {
              ({ cmd, size } = yield byteRange());
            } while (size < bufferLength);
          }
        } while (!done);
      } catch (e) {
        (threw = true) && typeof it.throw === "function" && it.throw(e);
      } finally {
        threw === false && typeof it.return === "function" && it.return(null);
      }
      return null;
    }
    function fromAsyncIterable(source) {
      return tslib_1.__asyncGenerator(this, arguments, function* fromAsyncIterable_1() {
        let done, threw = false;
        let buffers = [], buffer;
        let cmd, size, bufferLength = 0;
        function byteRange() {
          if (cmd === "peek") {
            return (0, buffer_js_1.joinUint8Arrays)(buffers, size)[0];
          }
          [buffer, buffers, bufferLength] = (0, buffer_js_1.joinUint8Arrays)(buffers, size);
          return buffer;
        }
        ({ cmd, size } = (yield yield tslib_1.__await(/* @__PURE__ */ (() => null)())) || { cmd: "read", size: 0 });
        const it = (0, buffer_js_1.toUint8ArrayAsyncIterator)(source)[Symbol.asyncIterator]();
        try {
          do {
            ({ done, value: buffer } = Number.isNaN(size - bufferLength) ? yield tslib_1.__await(it.next()) : yield tslib_1.__await(it.next(size - bufferLength)));
            if (!done && buffer.byteLength > 0) {
              buffers.push(buffer);
              bufferLength += buffer.byteLength;
            }
            if (done || size <= bufferLength) {
              do {
                ({ cmd, size } = yield yield tslib_1.__await(byteRange()));
              } while (size < bufferLength);
            }
          } while (!done);
        } catch (e) {
          (threw = true) && typeof it.throw === "function" && (yield tslib_1.__await(it.throw(e)));
        } finally {
          threw === false && typeof it.return === "function" && (yield tslib_1.__await(it.return(new Uint8Array(0))));
        }
        return yield tslib_1.__await(null);
      });
    }
    function fromDOMStream(source) {
      return tslib_1.__asyncGenerator(this, arguments, function* fromDOMStream_1() {
        let done = false, threw = false;
        let buffers = [], buffer;
        let cmd, size, bufferLength = 0;
        function byteRange() {
          if (cmd === "peek") {
            return (0, buffer_js_1.joinUint8Arrays)(buffers, size)[0];
          }
          [buffer, buffers, bufferLength] = (0, buffer_js_1.joinUint8Arrays)(buffers, size);
          return buffer;
        }
        ({ cmd, size } = (yield yield tslib_1.__await(/* @__PURE__ */ (() => null)())) || { cmd: "read", size: 0 });
        const it = new AdaptiveByteReader(source);
        try {
          do {
            ({ done, value: buffer } = Number.isNaN(size - bufferLength) ? yield tslib_1.__await(it["read"]()) : yield tslib_1.__await(it["read"](size - bufferLength)));
            if (!done && buffer.byteLength > 0) {
              buffers.push((0, buffer_js_1.toUint8Array)(buffer));
              bufferLength += buffer.byteLength;
            }
            if (done || size <= bufferLength) {
              do {
                ({ cmd, size } = yield yield tslib_1.__await(byteRange()));
              } while (size < bufferLength);
            }
          } while (!done);
        } catch (e) {
          (threw = true) && (yield tslib_1.__await(it["cancel"](e)));
        } finally {
          threw === false ? yield tslib_1.__await(it["cancel"]()) : source["locked"] && it.releaseLock();
        }
        return yield tslib_1.__await(null);
      });
    }
    var AdaptiveByteReader = class {
      constructor(source) {
        this.source = source;
        this.reader = null;
        this.reader = this.source["getReader"]();
        this.reader["closed"].catch(() => {
        });
      }
      get closed() {
        return this.reader ? this.reader["closed"].catch(() => {
        }) : Promise.resolve();
      }
      releaseLock() {
        if (this.reader) {
          this.reader.releaseLock();
        }
        this.reader = null;
      }
      cancel(reason) {
        return tslib_1.__awaiter(this, void 0, void 0, function* () {
          const { reader, source } = this;
          reader && (yield reader["cancel"](reason).catch(() => {
          }));
          source && (source["locked"] && this.releaseLock());
        });
      }
      read(size) {
        return tslib_1.__awaiter(this, void 0, void 0, function* () {
          if (size === 0) {
            return { done: this.reader == null, value: new Uint8Array(0) };
          }
          const result = yield this.reader.read();
          !result.done && (result.value = (0, buffer_js_1.toUint8Array)(result));
          return result;
        });
      }
    };
    var onEvent = (stream, event) => {
      const handler = (_) => resolve([event, _]);
      let resolve;
      return [event, handler, new Promise((r) => (resolve = r) && stream["once"](event, handler))];
    };
    function fromNodeStream(stream) {
      return tslib_1.__asyncGenerator(this, arguments, function* fromNodeStream_1() {
        const events = [];
        let event = "error";
        let done = false, err = null;
        let cmd, size, bufferLength = 0;
        let buffers = [], buffer;
        function byteRange() {
          if (cmd === "peek") {
            return (0, buffer_js_1.joinUint8Arrays)(buffers, size)[0];
          }
          [buffer, buffers, bufferLength] = (0, buffer_js_1.joinUint8Arrays)(buffers, size);
          return buffer;
        }
        ({ cmd, size } = (yield yield tslib_1.__await(/* @__PURE__ */ (() => null)())) || { cmd: "read", size: 0 });
        if (stream["isTTY"]) {
          yield yield tslib_1.__await(new Uint8Array(0));
          return yield tslib_1.__await(null);
        }
        try {
          events[0] = onEvent(stream, "end");
          events[1] = onEvent(stream, "error");
          do {
            events[2] = onEvent(stream, "readable");
            [event, err] = yield tslib_1.__await(Promise.race(events.map((x) => x[2])));
            if (event === "error") {
              break;
            }
            if (!(done = event === "end")) {
              if (!Number.isFinite(size - bufferLength)) {
                buffer = (0, buffer_js_1.toUint8Array)(stream["read"]());
              } else {
                buffer = (0, buffer_js_1.toUint8Array)(stream["read"](size - bufferLength));
                if (buffer.byteLength < size - bufferLength) {
                  buffer = (0, buffer_js_1.toUint8Array)(stream["read"]());
                }
              }
              if (buffer.byteLength > 0) {
                buffers.push(buffer);
                bufferLength += buffer.byteLength;
              }
            }
            if (done || size <= bufferLength) {
              do {
                ({ cmd, size } = yield yield tslib_1.__await(byteRange()));
              } while (size < bufferLength);
            }
          } while (!done);
        } finally {
          yield tslib_1.__await(cleanup(events, event === "error" ? err : null));
        }
        return yield tslib_1.__await(null);
        function cleanup(events2, err2) {
          buffer = buffers = null;
          return new Promise((resolve, reject) => {
            for (const [evt, fn] of events2) {
              stream["off"](evt, fn);
            }
            try {
              const destroy = stream["destroy"];
              destroy && destroy.call(stream, err2);
              err2 = void 0;
            } catch (e) {
              err2 = e || err2;
            } finally {
              err2 != null ? reject(err2) : resolve();
            }
          });
        }
      });
    }
  }
});

// node_modules/apache-arrow/fb/metadata-version.js
var require_metadata_version = __commonJS({
  "node_modules/apache-arrow/fb/metadata-version.js"(exports2) {
    "use strict";
    Object.defineProperty(exports2, "__esModule", { value: true });
    exports2.MetadataVersion = void 0;
    var MetadataVersion;
    (function(MetadataVersion2) {
      MetadataVersion2[MetadataVersion2["V1"] = 0] = "V1";
      MetadataVersion2[MetadataVersion2["V2"] = 1] = "V2";
      MetadataVersion2[MetadataVersion2["V3"] = 2] = "V3";
      MetadataVersion2[MetadataVersion2["V4"] = 3] = "V4";
      MetadataVersion2[MetadataVersion2["V5"] = 4] = "V5";
    })(MetadataVersion || (exports2.MetadataVersion = MetadataVersion = {}));
  }
});

// node_modules/apache-arrow/fb/union-mode.js
var require_union_mode = __commonJS({
  "node_modules/apache-arrow/fb/union-mode.js"(exports2) {
    "use strict";
    Object.defineProperty(exports2, "__esModule", { value: true });
    exports2.UnionMode = void 0;
    var UnionMode;
    (function(UnionMode2) {
      UnionMode2[UnionMode2["Sparse"] = 0] = "Sparse";
      UnionMode2[UnionMode2["Dense"] = 1] = "Dense";
    })(UnionMode || (exports2.UnionMode = UnionMode = {}));
  }
});

// node_modules/apache-arrow/fb/precision.js
var require_precision = __commonJS({
  "node_modules/apache-arrow/fb/precision.js"(exports2) {
    "use strict";
    Object.defineProperty(exports2, "__esModule", { value: true });
    exports2.Precision = void 0;
    var Precision;
    (function(Precision2) {
      Precision2[Precision2["HALF"] = 0] = "HALF";
      Precision2[Precision2["SINGLE"] = 1] = "SINGLE";
      Precision2[Precision2["DOUBLE"] = 2] = "DOUBLE";
    })(Precision || (exports2.Precision = Precision = {}));
  }
});

// node_modules/apache-arrow/fb/date-unit.js
var require_date_unit = __commonJS({
  "node_modules/apache-arrow/fb/date-unit.js"(exports2) {
    "use strict";
    Object.defineProperty(exports2, "__esModule", { value: true });
    exports2.DateUnit = void 0;
    var DateUnit;
    (function(DateUnit2) {
      DateUnit2[DateUnit2["DAY"] = 0] = "DAY";
      DateUnit2[DateUnit2["MILLISECOND"] = 1] = "MILLISECOND";
    })(DateUnit || (exports2.DateUnit = DateUnit = {}));
  }
});

// node_modules/apache-arrow/fb/time-unit.js
var require_time_unit = __commonJS({
  "node_modules/apache-arrow/fb/time-unit.js"(exports2) {
    "use strict";
    Object.defineProperty(exports2, "__esModule", { value: true });
    exports2.TimeUnit = void 0;
    var TimeUnit;
    (function(TimeUnit2) {
      TimeUnit2[TimeUnit2["SECOND"] = 0] = "SECOND";
      TimeUnit2[TimeUnit2["MILLISECOND"] = 1] = "MILLISECOND";
      TimeUnit2[TimeUnit2["MICROSECOND"] = 2] = "MICROSECOND";
      TimeUnit2[TimeUnit2["NANOSECOND"] = 3] = "NANOSECOND";
    })(TimeUnit || (exports2.TimeUnit = TimeUnit = {}));
  }
});

// node_modules/apache-arrow/fb/interval-unit.js
var require_interval_unit = __commonJS({
  "node_modules/apache-arrow/fb/interval-unit.js"(exports2) {
    "use strict";
    Object.defineProperty(exports2, "__esModule", { value: true });
    exports2.IntervalUnit = void 0;
    var IntervalUnit;
    (function(IntervalUnit2) {
      IntervalUnit2[IntervalUnit2["YEAR_MONTH"] = 0] = "YEAR_MONTH";
      IntervalUnit2[IntervalUnit2["DAY_TIME"] = 1] = "DAY_TIME";
      IntervalUnit2[IntervalUnit2["MONTH_DAY_NANO"] = 2] = "MONTH_DAY_NANO";
    })(IntervalUnit || (exports2.IntervalUnit = IntervalUnit = {}));
  }
});

// node_modules/flatbuffers/mjs/constants.js
var SIZEOF_SHORT, SIZEOF_INT, FILE_IDENTIFIER_LENGTH, SIZE_PREFIX_LENGTH;
var init_constants = __esm({
  "node_modules/flatbuffers/mjs/constants.js"() {
    "use strict";
    SIZEOF_SHORT = 2;
    SIZEOF_INT = 4;
    FILE_IDENTIFIER_LENGTH = 4;
    SIZE_PREFIX_LENGTH = 4;
  }
});

// node_modules/flatbuffers/mjs/utils.js
var int32, float32, float64, isLittleEndian;
var init_utils = __esm({
  "node_modules/flatbuffers/mjs/utils.js"() {
    "use strict";
    int32 = new Int32Array(2);
    float32 = new Float32Array(int32.buffer);
    float64 = new Float64Array(int32.buffer);
    isLittleEndian = new Uint16Array(new Uint8Array([1, 0]).buffer)[0] === 1;
  }
});

// node_modules/flatbuffers/mjs/encoding.js
var Encoding;
var init_encoding = __esm({
  "node_modules/flatbuffers/mjs/encoding.js"() {
    "use strict";
    (function(Encoding2) {
      Encoding2[Encoding2["UTF8_BYTES"] = 1] = "UTF8_BYTES";
      Encoding2[Encoding2["UTF16_STRING"] = 2] = "UTF16_STRING";
    })(Encoding || (Encoding = {}));
  }
});

// node_modules/flatbuffers/mjs/byte-buffer.js
var ByteBuffer;
var init_byte_buffer = __esm({
  "node_modules/flatbuffers/mjs/byte-buffer.js"() {
    "use strict";
    init_constants();
    init_utils();
    init_encoding();
    ByteBuffer = class _ByteBuffer {
      /**
       * Create a new ByteBuffer with a given array of bytes (`Uint8Array`)
       */
      constructor(bytes_) {
        this.bytes_ = bytes_;
        this.position_ = 0;
        this.text_decoder_ = new TextDecoder();
      }
      /**
       * Create and allocate a new ByteBuffer with a given size.
       */
      static allocate(byte_size) {
        return new _ByteBuffer(new Uint8Array(byte_size));
      }
      clear() {
        this.position_ = 0;
      }
      /**
       * Get the underlying `Uint8Array`.
       */
      bytes() {
        return this.bytes_;
      }
      /**
       * Get the buffer's position.
       */
      position() {
        return this.position_;
      }
      /**
       * Set the buffer's position.
       */
      setPosition(position) {
        this.position_ = position;
      }
      /**
       * Get the buffer's capacity.
       */
      capacity() {
        return this.bytes_.length;
      }
      readInt8(offset) {
        return this.readUint8(offset) << 24 >> 24;
      }
      readUint8(offset) {
        return this.bytes_[offset];
      }
      readInt16(offset) {
        return this.readUint16(offset) << 16 >> 16;
      }
      readUint16(offset) {
        return this.bytes_[offset] | this.bytes_[offset + 1] << 8;
      }
      readInt32(offset) {
        return this.bytes_[offset] | this.bytes_[offset + 1] << 8 | this.bytes_[offset + 2] << 16 | this.bytes_[offset + 3] << 24;
      }
      readUint32(offset) {
        return this.readInt32(offset) >>> 0;
      }
      readInt64(offset) {
        return BigInt.asIntN(64, BigInt(this.readUint32(offset)) + (BigInt(this.readUint32(offset + 4)) << BigInt(32)));
      }
      readUint64(offset) {
        return BigInt.asUintN(64, BigInt(this.readUint32(offset)) + (BigInt(this.readUint32(offset + 4)) << BigInt(32)));
      }
      readFloat32(offset) {
        int32[0] = this.readInt32(offset);
        return float32[0];
      }
      readFloat64(offset) {
        int32[isLittleEndian ? 0 : 1] = this.readInt32(offset);
        int32[isLittleEndian ? 1 : 0] = this.readInt32(offset + 4);
        return float64[0];
      }
      writeInt8(offset, value) {
        this.bytes_[offset] = value;
      }
      writeUint8(offset, value) {
        this.bytes_[offset] = value;
      }
      writeInt16(offset, value) {
        this.bytes_[offset] = value;
        this.bytes_[offset + 1] = value >> 8;
      }
      writeUint16(offset, value) {
        this.bytes_[offset] = value;
        this.bytes_[offset + 1] = value >> 8;
      }
      writeInt32(offset, value) {
        this.bytes_[offset] = value;
        this.bytes_[offset + 1] = value >> 8;
        this.bytes_[offset + 2] = value >> 16;
        this.bytes_[offset + 3] = value >> 24;
      }
      writeUint32(offset, value) {
        this.bytes_[offset] = value;
        this.bytes_[offset + 1] = value >> 8;
        this.bytes_[offset + 2] = value >> 16;
        this.bytes_[offset + 3] = value >> 24;
      }
      writeInt64(offset, value) {
        this.writeInt32(offset, Number(BigInt.asIntN(32, value)));
        this.writeInt32(offset + 4, Number(BigInt.asIntN(32, value >> BigInt(32))));
      }
      writeUint64(offset, value) {
        this.writeUint32(offset, Number(BigInt.asUintN(32, value)));
        this.writeUint32(offset + 4, Number(BigInt.asUintN(32, value >> BigInt(32))));
      }
      writeFloat32(offset, value) {
        float32[0] = value;
        this.writeInt32(offset, int32[0]);
      }
      writeFloat64(offset, value) {
        float64[0] = value;
        this.writeInt32(offset, int32[isLittleEndian ? 0 : 1]);
        this.writeInt32(offset + 4, int32[isLittleEndian ? 1 : 0]);
      }
      /**
       * Return the file identifier.   Behavior is undefined for FlatBuffers whose
       * schema does not include a file_identifier (likely points at padding or the
       * start of a the root vtable).
       */
      getBufferIdentifier() {
        if (this.bytes_.length < this.position_ + SIZEOF_INT + FILE_IDENTIFIER_LENGTH) {
          throw new Error("FlatBuffers: ByteBuffer is too short to contain an identifier.");
        }
        let result = "";
        for (let i = 0; i < FILE_IDENTIFIER_LENGTH; i++) {
          result += String.fromCharCode(this.readInt8(this.position_ + SIZEOF_INT + i));
        }
        return result;
      }
      /**
       * Look up a field in the vtable, return an offset into the object, or 0 if the
       * field is not present.
       */
      __offset(bb_pos, vtable_offset) {
        const vtable = bb_pos - this.readInt32(bb_pos);
        return vtable_offset < this.readInt16(vtable) ? this.readInt16(vtable + vtable_offset) : 0;
      }
      /**
       * Initialize any Table-derived type to point to the union at the given offset.
       */
      __union(t, offset) {
        t.bb_pos = offset + this.readInt32(offset);
        t.bb = this;
        return t;
      }
      /**
       * Create a JavaScript string from UTF-8 data stored inside the FlatBuffer.
       * This allocates a new string and converts to wide chars upon each access.
       *
       * To avoid the conversion to string, pass Encoding.UTF8_BYTES as the
       * "optionalEncoding" argument. This is useful for avoiding conversion when
       * the data will just be packaged back up in another FlatBuffer later on.
       *
       * @param offset
       * @param opt_encoding Defaults to UTF16_STRING
       */
      __string(offset, opt_encoding) {
        offset += this.readInt32(offset);
        const length = this.readInt32(offset);
        offset += SIZEOF_INT;
        const utf8bytes = this.bytes_.subarray(offset, offset + length);
        if (opt_encoding === Encoding.UTF8_BYTES)
          return utf8bytes;
        else
          return this.text_decoder_.decode(utf8bytes);
      }
      /**
       * Handle unions that can contain string as its member, if a Table-derived type then initialize it,
       * if a string then return a new one
       *
       * WARNING: strings are immutable in JS so we can't change the string that the user gave us, this
       * makes the behaviour of __union_with_string different compared to __union
       */
      __union_with_string(o, offset) {
        if (typeof o === "string") {
          return this.__string(offset);
        }
        return this.__union(o, offset);
      }
      /**
       * Retrieve the relative offset stored at "offset"
       */
      __indirect(offset) {
        return offset + this.readInt32(offset);
      }
      /**
       * Get the start of data of a vector whose offset is stored at "offset" in this object.
       */
      __vector(offset) {
        return offset + this.readInt32(offset) + SIZEOF_INT;
      }
      /**
       * Get the length of a vector whose offset is stored at "offset" in this object.
       */
      __vector_len(offset) {
        return this.readInt32(offset + this.readInt32(offset));
      }
      __has_identifier(ident) {
        if (ident.length != FILE_IDENTIFIER_LENGTH) {
          throw new Error("FlatBuffers: file identifier must be length " + FILE_IDENTIFIER_LENGTH);
        }
        for (let i = 0; i < FILE_IDENTIFIER_LENGTH; i++) {
          if (ident.charCodeAt(i) != this.readInt8(this.position() + SIZEOF_INT + i)) {
            return false;
          }
        }
        return true;
      }
      /**
       * A helper function for generating list for obj api
       */
      createScalarList(listAccessor, listLength) {
        const ret = [];
        for (let i = 0; i < listLength; ++i) {
          const val = listAccessor(i);
          if (val !== null) {
            ret.push(val);
          }
        }
        return ret;
      }
      /**
       * A helper function for generating list for obj api
       * @param listAccessor function that accepts an index and return data at that index
       * @param listLength listLength
       * @param res result list
       */
      createObjList(listAccessor, listLength) {
        const ret = [];
        for (let i = 0; i < listLength; ++i) {
          const val = listAccessor(i);
          if (val !== null) {
            ret.push(val.unpack());
          }
        }
        return ret;
      }
    };
  }
});

// node_modules/flatbuffers/mjs/builder.js
var Builder;
var init_builder = __esm({
  "node_modules/flatbuffers/mjs/builder.js"() {
    "use strict";
    init_byte_buffer();
    init_constants();
    Builder = class _Builder {
      /**
       * Create a FlatBufferBuilder.
       */
      constructor(opt_initial_size) {
        this.minalign = 1;
        this.vtable = null;
        this.vtable_in_use = 0;
        this.isNested = false;
        this.object_start = 0;
        this.vtables = [];
        this.vector_num_elems = 0;
        this.force_defaults = false;
        this.string_maps = null;
        this.text_encoder = new TextEncoder();
        let initial_size;
        if (!opt_initial_size) {
          initial_size = 1024;
        } else {
          initial_size = opt_initial_size;
        }
        this.bb = ByteBuffer.allocate(initial_size);
        this.space = initial_size;
      }
      clear() {
        this.bb.clear();
        this.space = this.bb.capacity();
        this.minalign = 1;
        this.vtable = null;
        this.vtable_in_use = 0;
        this.isNested = false;
        this.object_start = 0;
        this.vtables = [];
        this.vector_num_elems = 0;
        this.force_defaults = false;
        this.string_maps = null;
      }
      /**
       * In order to save space, fields that are set to their default value
       * don't get serialized into the buffer. Forcing defaults provides a
       * way to manually disable this optimization.
       *
       * @param forceDefaults true always serializes default values
       */
      forceDefaults(forceDefaults) {
        this.force_defaults = forceDefaults;
      }
      /**
       * Get the ByteBuffer representing the FlatBuffer. Only call this after you've
       * called finish(). The actual data starts at the ByteBuffer's current position,
       * not necessarily at 0.
       */
      dataBuffer() {
        return this.bb;
      }
      /**
       * Get the bytes representing the FlatBuffer. Only call this after you've
       * called finish().
       */
      asUint8Array() {
        return this.bb.bytes().subarray(this.bb.position(), this.bb.position() + this.offset());
      }
      /**
       * Prepare to write an element of `size` after `additional_bytes` have been
       * written, e.g. if you write a string, you need to align such the int length
       * field is aligned to 4 bytes, and the string data follows it directly. If all
       * you need to do is alignment, `additional_bytes` will be 0.
       *
       * @param size This is the of the new element to write
       * @param additional_bytes The padding size
       */
      prep(size, additional_bytes) {
        if (size > this.minalign) {
          this.minalign = size;
        }
        const align_size = ~(this.bb.capacity() - this.space + additional_bytes) + 1 & size - 1;
        while (this.space < align_size + size + additional_bytes) {
          const old_buf_size = this.bb.capacity();
          this.bb = _Builder.growByteBuffer(this.bb);
          this.space += this.bb.capacity() - old_buf_size;
        }
        this.pad(align_size);
      }
      pad(byte_size) {
        for (let i = 0; i < byte_size; i++) {
          this.bb.writeInt8(--this.space, 0);
        }
      }
      writeInt8(value) {
        this.bb.writeInt8(this.space -= 1, value);
      }
      writeInt16(value) {
        this.bb.writeInt16(this.space -= 2, value);
      }
      writeInt32(value) {
        this.bb.writeInt32(this.space -= 4, value);
      }
      writeInt64(value) {
        this.bb.writeInt64(this.space -= 8, value);
      }
      writeFloat32(value) {
        this.bb.writeFloat32(this.space -= 4, value);
      }
      writeFloat64(value) {
        this.bb.writeFloat64(this.space -= 8, value);
      }
      /**
       * Add an `int8` to the buffer, properly aligned, and grows the buffer (if necessary).
       * @param value The `int8` to add the buffer.
       */
      addInt8(value) {
        this.prep(1, 0);
        this.writeInt8(value);
      }
      /**
       * Add an `int16` to the buffer, properly aligned, and grows the buffer (if necessary).
       * @param value The `int16` to add the buffer.
       */
      addInt16(value) {
        this.prep(2, 0);
        this.writeInt16(value);
      }
      /**
       * Add an `int32` to the buffer, properly aligned, and grows the buffer (if necessary).
       * @param value The `int32` to add the buffer.
       */
      addInt32(value) {
        this.prep(4, 0);
        this.writeInt32(value);
      }
      /**
       * Add an `int64` to the buffer, properly aligned, and grows the buffer (if necessary).
       * @param value The `int64` to add the buffer.
       */
      addInt64(value) {
        this.prep(8, 0);
        this.writeInt64(value);
      }
      /**
       * Add a `float32` to the buffer, properly aligned, and grows the buffer (if necessary).
       * @param value The `float32` to add the buffer.
       */
      addFloat32(value) {
        this.prep(4, 0);
        this.writeFloat32(value);
      }
      /**
       * Add a `float64` to the buffer, properly aligned, and grows the buffer (if necessary).
       * @param value The `float64` to add the buffer.
       */
      addFloat64(value) {
        this.prep(8, 0);
        this.writeFloat64(value);
      }
      addFieldInt8(voffset, value, defaultValue) {
        if (this.force_defaults || value != defaultValue) {
          this.addInt8(value);
          this.slot(voffset);
        }
      }
      addFieldInt16(voffset, value, defaultValue) {
        if (this.force_defaults || value != defaultValue) {
          this.addInt16(value);
          this.slot(voffset);
        }
      }
      addFieldInt32(voffset, value, defaultValue) {
        if (this.force_defaults || value != defaultValue) {
          this.addInt32(value);
          this.slot(voffset);
        }
      }
      addFieldInt64(voffset, value, defaultValue) {
        if (this.force_defaults || value !== defaultValue) {
          this.addInt64(value);
          this.slot(voffset);
        }
      }
      addFieldFloat32(voffset, value, defaultValue) {
        if (this.force_defaults || value != defaultValue) {
          this.addFloat32(value);
          this.slot(voffset);
        }
      }
      addFieldFloat64(voffset, value, defaultValue) {
        if (this.force_defaults || value != defaultValue) {
          this.addFloat64(value);
          this.slot(voffset);
        }
      }
      addFieldOffset(voffset, value, defaultValue) {
        if (this.force_defaults || value != defaultValue) {
          this.addOffset(value);
          this.slot(voffset);
        }
      }
      /**
       * Structs are stored inline, so nothing additional is being added. `d` is always 0.
       */
      addFieldStruct(voffset, value, defaultValue) {
        if (value != defaultValue) {
          this.nested(value);
          this.slot(voffset);
        }
      }
      /**
       * Structures are always stored inline, they need to be created right
       * where they're used.  You'll get this assertion failure if you
       * created it elsewhere.
       */
      nested(obj) {
        if (obj != this.offset()) {
          throw new TypeError("FlatBuffers: struct must be serialized inline.");
        }
      }
      /**
       * Should not be creating any other object, string or vector
       * while an object is being constructed
       */
      notNested() {
        if (this.isNested) {
          throw new TypeError("FlatBuffers: object serialization must not be nested.");
        }
      }
      /**
       * Set the current vtable at `voffset` to the current location in the buffer.
       */
      slot(voffset) {
        if (this.vtable !== null)
          this.vtable[voffset] = this.offset();
      }
      /**
       * @returns Offset relative to the end of the buffer.
       */
      offset() {
        return this.bb.capacity() - this.space;
      }
      /**
       * Doubles the size of the backing ByteBuffer and copies the old data towards
       * the end of the new buffer (since we build the buffer backwards).
       *
       * @param bb The current buffer with the existing data
       * @returns A new byte buffer with the old data copied
       * to it. The data is located at the end of the buffer.
       *
       * uint8Array.set() formally takes {Array<number>|ArrayBufferView}, so to pass
       * it a uint8Array we need to suppress the type check:
       * @suppress {checkTypes}
       */
      static growByteBuffer(bb) {
        const old_buf_size = bb.capacity();
        if (old_buf_size & 3221225472) {
          throw new Error("FlatBuffers: cannot grow buffer beyond 2 gigabytes.");
        }
        const new_buf_size = old_buf_size << 1;
        const nbb = ByteBuffer.allocate(new_buf_size);
        nbb.setPosition(new_buf_size - old_buf_size);
        nbb.bytes().set(bb.bytes(), new_buf_size - old_buf_size);
        return nbb;
      }
      /**
       * Adds on offset, relative to where it will be written.
       *
       * @param offset The offset to add.
       */
      addOffset(offset) {
        this.prep(SIZEOF_INT, 0);
        this.writeInt32(this.offset() - offset + SIZEOF_INT);
      }
      /**
       * Start encoding a new object in the buffer.  Users will not usually need to
       * call this directly. The FlatBuffers compiler will generate helper methods
       * that call this method internally.
       */
      startObject(numfields) {
        this.notNested();
        if (this.vtable == null) {
          this.vtable = [];
        }
        this.vtable_in_use = numfields;
        for (let i = 0; i < numfields; i++) {
          this.vtable[i] = 0;
        }
        this.isNested = true;
        this.object_start = this.offset();
      }
      /**
       * Finish off writing the object that is under construction.
       *
       * @returns The offset to the object inside `dataBuffer`
       */
      endObject() {
        if (this.vtable == null || !this.isNested) {
          throw new Error("FlatBuffers: endObject called without startObject");
        }
        this.addInt32(0);
        const vtableloc = this.offset();
        let i = this.vtable_in_use - 1;
        for (; i >= 0 && this.vtable[i] == 0; i--) {
        }
        const trimmed_size = i + 1;
        for (; i >= 0; i--) {
          this.addInt16(this.vtable[i] != 0 ? vtableloc - this.vtable[i] : 0);
        }
        const standard_fields = 2;
        this.addInt16(vtableloc - this.object_start);
        const len = (trimmed_size + standard_fields) * SIZEOF_SHORT;
        this.addInt16(len);
        let existing_vtable = 0;
        const vt1 = this.space;
        outer_loop: for (i = 0; i < this.vtables.length; i++) {
          const vt2 = this.bb.capacity() - this.vtables[i];
          if (len == this.bb.readInt16(vt2)) {
            for (let j = SIZEOF_SHORT; j < len; j += SIZEOF_SHORT) {
              if (this.bb.readInt16(vt1 + j) != this.bb.readInt16(vt2 + j)) {
                continue outer_loop;
              }
            }
            existing_vtable = this.vtables[i];
            break;
          }
        }
        if (existing_vtable) {
          this.space = this.bb.capacity() - vtableloc;
          this.bb.writeInt32(this.space, existing_vtable - vtableloc);
        } else {
          this.vtables.push(this.offset());
          this.bb.writeInt32(this.bb.capacity() - vtableloc, this.offset() - vtableloc);
        }
        this.isNested = false;
        return vtableloc;
      }
      /**
       * Finalize a buffer, poiting to the given `root_table`.
       */
      finish(root_table, opt_file_identifier, opt_size_prefix) {
        const size_prefix = opt_size_prefix ? SIZE_PREFIX_LENGTH : 0;
        if (opt_file_identifier) {
          const file_identifier = opt_file_identifier;
          this.prep(this.minalign, SIZEOF_INT + FILE_IDENTIFIER_LENGTH + size_prefix);
          if (file_identifier.length != FILE_IDENTIFIER_LENGTH) {
            throw new TypeError("FlatBuffers: file identifier must be length " + FILE_IDENTIFIER_LENGTH);
          }
          for (let i = FILE_IDENTIFIER_LENGTH - 1; i >= 0; i--) {
            this.writeInt8(file_identifier.charCodeAt(i));
          }
        }
        this.prep(this.minalign, SIZEOF_INT + size_prefix);
        this.addOffset(root_table);
        if (size_prefix) {
          this.addInt32(this.bb.capacity() - this.space);
        }
        this.bb.setPosition(this.space);
      }
      /**
       * Finalize a size prefixed buffer, pointing to the given `root_table`.
       */
      finishSizePrefixed(root_table, opt_file_identifier) {
        this.finish(root_table, opt_file_identifier, true);
      }
      /**
       * This checks a required field has been set in a given table that has
       * just been constructed.
       */
      requiredField(table, field) {
        const table_start = this.bb.capacity() - table;
        const vtable_start = table_start - this.bb.readInt32(table_start);
        const ok = field < this.bb.readInt16(vtable_start) && this.bb.readInt16(vtable_start + field) != 0;
        if (!ok) {
          throw new TypeError("FlatBuffers: field " + field + " must be set");
        }
      }
      /**
       * Start a new array/vector of objects.  Users usually will not call
       * this directly. The FlatBuffers compiler will create a start/end
       * method for vector types in generated code.
       *
       * @param elem_size The size of each element in the array
       * @param num_elems The number of elements in the array
       * @param alignment The alignment of the array
       */
      startVector(elem_size, num_elems, alignment) {
        this.notNested();
        this.vector_num_elems = num_elems;
        this.prep(SIZEOF_INT, elem_size * num_elems);
        this.prep(alignment, elem_size * num_elems);
      }
      /**
       * Finish off the creation of an array and all its elements. The array must be
       * created with `startVector`.
       *
       * @returns The offset at which the newly created array
       * starts.
       */
      endVector() {
        this.writeInt32(this.vector_num_elems);
        return this.offset();
      }
      /**
       * Encode the string `s` in the buffer using UTF-8. If the string passed has
       * already been seen, we return the offset of the already written string
       *
       * @param s The string to encode
       * @return The offset in the buffer where the encoded string starts
       */
      createSharedString(s) {
        if (!s) {
          return 0;
        }
        if (!this.string_maps) {
          this.string_maps = /* @__PURE__ */ new Map();
        }
        if (this.string_maps.has(s)) {
          return this.string_maps.get(s);
        }
        const offset = this.createString(s);
        this.string_maps.set(s, offset);
        return offset;
      }
      /**
       * Encode the string `s` in the buffer using UTF-8. If a Uint8Array is passed
       * instead of a string, it is assumed to contain valid UTF-8 encoded data.
       *
       * @param s The string to encode
       * @return The offset in the buffer where the encoded string starts
       */
      createString(s) {
        if (s === null || s === void 0) {
          return 0;
        }
        let utf8;
        if (s instanceof Uint8Array) {
          utf8 = s;
        } else {
          utf8 = this.text_encoder.encode(s);
        }
        this.addInt8(0);
        this.startVector(1, utf8.length, 1);
        this.bb.setPosition(this.space -= utf8.length);
        this.bb.bytes().set(utf8, this.space);
        return this.endVector();
      }
      /**
       * Create a byte vector.
       *
       * @param v The bytes to add
       * @returns The offset in the buffer where the byte vector starts
       */
      createByteVector(v) {
        if (v === null || v === void 0) {
          return 0;
        }
        this.startVector(1, v.length, 1);
        this.bb.setPosition(this.space -= v.length);
        this.bb.bytes().set(v, this.space);
        return this.endVector();
      }
      /**
       * A helper function to pack an object
       *
       * @returns offset of obj
       */
      createObjectOffset(obj) {
        if (obj === null) {
          return 0;
        }
        if (typeof obj === "string") {
          return this.createString(obj);
        } else {
          return obj.pack(this);
        }
      }
      /**
       * A helper function to pack a list of object
       *
       * @returns list of offsets of each non null object
       */
      createObjectOffsetList(list) {
        const ret = [];
        for (let i = 0; i < list.length; ++i) {
          const val = list[i];
          if (val !== null) {
            ret.push(this.createObjectOffset(val));
          } else {
            throw new TypeError("FlatBuffers: Argument for createObjectOffsetList cannot contain null.");
          }
        }
        return ret;
      }
      createStructOffsetList(list, startFunc) {
        startFunc(this, list.length);
        this.createObjectOffsetList(list.slice().reverse());
        return this.endVector();
      }
    };
  }
});

// node_modules/flatbuffers/mjs/flatbuffers.js
var flatbuffers_exports = {};
__export(flatbuffers_exports, {
  Builder: () => Builder,
  ByteBuffer: () => ByteBuffer,
  Encoding: () => Encoding,
  FILE_IDENTIFIER_LENGTH: () => FILE_IDENTIFIER_LENGTH,
  SIZEOF_INT: () => SIZEOF_INT,
  SIZEOF_SHORT: () => SIZEOF_SHORT,
  SIZE_PREFIX_LENGTH: () => SIZE_PREFIX_LENGTH,
  float32: () => float32,
  float64: () => float64,
  int32: () => int32,
  isLittleEndian: () => isLittleEndian
});
var init_flatbuffers = __esm({
  "node_modules/flatbuffers/mjs/flatbuffers.js"() {
    "use strict";
    init_constants();
    init_constants();
    init_constants();
    init_constants();
    init_utils();
    init_encoding();
    init_builder();
    init_byte_buffer();
  }
});

// node_modules/apache-arrow/fb/body-compression-method.js
var require_body_compression_method = __commonJS({
  "node_modules/apache-arrow/fb/body-compression-method.js"(exports2) {
    "use strict";
    Object.defineProperty(exports2, "__esModule", { value: true });
    exports2.BodyCompressionMethod = void 0;
    var BodyCompressionMethod;
    (function(BodyCompressionMethod2) {
      BodyCompressionMethod2[BodyCompressionMethod2["BUFFER"] = 0] = "BUFFER";
    })(BodyCompressionMethod || (exports2.BodyCompressionMethod = BodyCompressionMethod = {}));
  }
});

// node_modules/apache-arrow/fb/compression-type.js
var require_compression_type = __commonJS({
  "node_modules/apache-arrow/fb/compression-type.js"(exports2) {
    "use strict";
    Object.defineProperty(exports2, "__esModule", { value: true });
    exports2.CompressionType = void 0;
    var CompressionType;
    (function(CompressionType2) {
      CompressionType2[CompressionType2["LZ4_FRAME"] = 0] = "LZ4_FRAME";
      CompressionType2[CompressionType2["ZSTD"] = 1] = "ZSTD";
    })(CompressionType || (exports2.CompressionType = CompressionType = {}));
  }
});

// node_modules/apache-arrow/fb/body-compression.js
var require_body_compression = __commonJS({
  "node_modules/apache-arrow/fb/body-compression.js"(exports2) {
    "use strict";
    Object.defineProperty(exports2, "__esModule", { value: true });
    exports2.BodyCompression = void 0;
    var flatbuffers = (init_flatbuffers(), __toCommonJS(flatbuffers_exports));
    var body_compression_method_js_1 = require_body_compression_method();
    var compression_type_js_1 = require_compression_type();
    var BodyCompression = class _BodyCompression {
      constructor() {
        this.bb = null;
        this.bb_pos = 0;
      }
      __init(i, bb) {
        this.bb_pos = i;
        this.bb = bb;
        return this;
      }
      static getRootAsBodyCompression(bb, obj) {
        return (obj || new _BodyCompression()).__init(bb.readInt32(bb.position()) + bb.position(), bb);
      }
      static getSizePrefixedRootAsBodyCompression(bb, obj) {
        bb.setPosition(bb.position() + flatbuffers.SIZE_PREFIX_LENGTH);
        return (obj || new _BodyCompression()).__init(bb.readInt32(bb.position()) + bb.position(), bb);
      }
      /**
       * Compressor library.
       * For LZ4_FRAME, each compressed buffer must consist of a single frame.
       */
      codec() {
        const offset = this.bb.__offset(this.bb_pos, 4);
        return offset ? this.bb.readInt8(this.bb_pos + offset) : compression_type_js_1.CompressionType.LZ4_FRAME;
      }
      /**
       * Indicates the way the record batch body was compressed
       */
      method() {
        const offset = this.bb.__offset(this.bb_pos, 6);
        return offset ? this.bb.readInt8(this.bb_pos + offset) : body_compression_method_js_1.BodyCompressionMethod.BUFFER;
      }
      static startBodyCompression(builder) {
        builder.startObject(2);
      }
      static addCodec(builder, codec) {
        builder.addFieldInt8(0, codec, compression_type_js_1.CompressionType.LZ4_FRAME);
      }
      static addMethod(builder, method) {
        builder.addFieldInt8(1, method, body_compression_method_js_1.BodyCompressionMethod.BUFFER);
      }
      static endBodyCompression(builder) {
        const offset = builder.endObject();
        return offset;
      }
      static createBodyCompression(builder, codec, method) {
        _BodyCompression.startBodyCompression(builder);
        _BodyCompression.addCodec(builder, codec);
        _BodyCompression.addMethod(builder, method);
        return _BodyCompression.endBodyCompression(builder);
      }
    };
    exports2.BodyCompression = BodyCompression;
  }
});

// node_modules/apache-arrow/fb/buffer.js
var require_buffer2 = __commonJS({
  "node_modules/apache-arrow/fb/buffer.js"(exports2) {
    "use strict";
    Object.defineProperty(exports2, "__esModule", { value: true });
    exports2.Buffer = void 0;
    var Buffer2 = class {
      constructor() {
        this.bb = null;
        this.bb_pos = 0;
      }
      __init(i, bb) {
        this.bb_pos = i;
        this.bb = bb;
        return this;
      }
      /**
       * The relative offset into the shared memory page where the bytes for this
       * buffer starts
       */
      offset() {
        return this.bb.readInt64(this.bb_pos);
      }
      /**
       * The absolute length (in bytes) of the memory buffer. The memory is found
       * from offset (inclusive) to offset + length (non-inclusive). When building
       * messages using the encapsulated IPC message, padding bytes may be written
       * after a buffer, but such padding bytes do not need to be accounted for in
       * the size here.
       */
      length() {
        return this.bb.readInt64(this.bb_pos + 8);
      }
      static sizeOf() {
        return 16;
      }
      static createBuffer(builder, offset, length) {
        builder.prep(8, 16);
        builder.writeInt64(BigInt(length !== null && length !== void 0 ? length : 0));
        builder.writeInt64(BigInt(offset !== null && offset !== void 0 ? offset : 0));
        return builder.offset();
      }
    };
    exports2.Buffer = Buffer2;
  }
});

// node_modules/apache-arrow/fb/field-node.js
var require_field_node = __commonJS({
  "node_modules/apache-arrow/fb/field-node.js"(exports2) {
    "use strict";
    Object.defineProperty(exports2, "__esModule", { value: true });
    exports2.FieldNode = void 0;
    var FieldNode = class {
      constructor() {
        this.bb = null;
        this.bb_pos = 0;
      }
      __init(i, bb) {
        this.bb_pos = i;
        this.bb = bb;
        return this;
      }
      /**
       * The number of value slots in the Arrow array at this level of a nested
       * tree
       */
      length() {
        return this.bb.readInt64(this.bb_pos);
      }
      /**
       * The number of observed nulls. Fields with null_count == 0 may choose not
       * to write their physical validity bitmap out as a materialized buffer,
       * instead setting the length of the bitmap buffer to 0.
       */
      nullCount() {
        return this.bb.readInt64(this.bb_pos + 8);
      }
      static sizeOf() {
        return 16;
      }
      static createFieldNode(builder, length, null_count) {
        builder.prep(8, 16);
        builder.writeInt64(BigInt(null_count !== null && null_count !== void 0 ? null_count : 0));
        builder.writeInt64(BigInt(length !== null && length !== void 0 ? length : 0));
        return builder.offset();
      }
    };
    exports2.FieldNode = FieldNode;
  }
});

// node_modules/apache-arrow/fb/record-batch.js
var require_record_batch = __commonJS({
  "node_modules/apache-arrow/fb/record-batch.js"(exports2) {
    "use strict";
    Object.defineProperty(exports2, "__esModule", { value: true });
    exports2.RecordBatch = void 0;
    var flatbuffers = (init_flatbuffers(), __toCommonJS(flatbuffers_exports));
    var body_compression_js_1 = require_body_compression();
    var buffer_js_1 = require_buffer2();
    var field_node_js_1 = require_field_node();
    var RecordBatch = class _RecordBatch {
      constructor() {
        this.bb = null;
        this.bb_pos = 0;
      }
      __init(i, bb) {
        this.bb_pos = i;
        this.bb = bb;
        return this;
      }
      static getRootAsRecordBatch(bb, obj) {
        return (obj || new _RecordBatch()).__init(bb.readInt32(bb.position()) + bb.position(), bb);
      }
      static getSizePrefixedRootAsRecordBatch(bb, obj) {
        bb.setPosition(bb.position() + flatbuffers.SIZE_PREFIX_LENGTH);
        return (obj || new _RecordBatch()).__init(bb.readInt32(bb.position()) + bb.position(), bb);
      }
      /**
       * number of records / rows. The arrays in the batch should all have this
       * length
       */
      length() {
        const offset = this.bb.__offset(this.bb_pos, 4);
        return offset ? this.bb.readInt64(this.bb_pos + offset) : BigInt("0");
      }
      /**
       * Nodes correspond to the pre-ordered flattened logical schema
       */
      nodes(index, obj) {
        const offset = this.bb.__offset(this.bb_pos, 6);
        return offset ? (obj || new field_node_js_1.FieldNode()).__init(this.bb.__vector(this.bb_pos + offset) + index * 16, this.bb) : null;
      }
      nodesLength() {
        const offset = this.bb.__offset(this.bb_pos, 6);
        return offset ? this.bb.__vector_len(this.bb_pos + offset) : 0;
      }
      /**
       * Buffers correspond to the pre-ordered flattened buffer tree
       *
       * The number of buffers appended to this list depends on the schema. For
       * example, most primitive arrays will have 2 buffers, 1 for the validity
       * bitmap and 1 for the values. For struct arrays, there will only be a
       * single buffer for the validity (nulls) bitmap
       */
      buffers(index, obj) {
        const offset = this.bb.__offset(this.bb_pos, 8);
        return offset ? (obj || new buffer_js_1.Buffer()).__init(this.bb.__vector(this.bb_pos + offset) + index * 16, this.bb) : null;
      }
      buffersLength() {
        const offset = this.bb.__offset(this.bb_pos, 8);
        return offset ? this.bb.__vector_len(this.bb_pos + offset) : 0;
      }
      /**
       * Optional compression of the message body
       */
      compression(obj) {
        const offset = this.bb.__offset(this.bb_pos, 10);
        return offset ? (obj || new body_compression_js_1.BodyCompression()).__init(this.bb.__indirect(this.bb_pos + offset), this.bb) : null;
      }
      static startRecordBatch(builder) {
        builder.startObject(4);
      }
      static addLength(builder, length) {
        builder.addFieldInt64(0, length, BigInt("0"));
      }
      static addNodes(builder, nodesOffset) {
        builder.addFieldOffset(1, nodesOffset, 0);
      }
      static startNodesVector(builder, numElems) {
        builder.startVector(16, numElems, 8);
      }
      static addBuffers(builder, buffersOffset) {
        builder.addFieldOffset(2, buffersOffset, 0);
      }
      static startBuffersVector(builder, numElems) {
        builder.startVector(16, numElems, 8);
      }
      static addCompression(builder, compressionOffset) {
        builder.addFieldOffset(3, compressionOffset, 0);
      }
      static endRecordBatch(builder) {
        const offset = builder.endObject();
        return offset;
      }
    };
    exports2.RecordBatch = RecordBatch;
  }
});

// node_modules/apache-arrow/fb/dictionary-batch.js
var require_dictionary_batch = __commonJS({
  "node_modules/apache-arrow/fb/dictionary-batch.js"(exports2) {
    "use strict";
    Object.defineProperty(exports2, "__esModule", { value: true });
    exports2.DictionaryBatch = void 0;
    var flatbuffers = (init_flatbuffers(), __toCommonJS(flatbuffers_exports));
    var record_batch_js_1 = require_record_batch();
    var DictionaryBatch = class _DictionaryBatch {
      constructor() {
        this.bb = null;
        this.bb_pos = 0;
      }
      __init(i, bb) {
        this.bb_pos = i;
        this.bb = bb;
        return this;
      }
      static getRootAsDictionaryBatch(bb, obj) {
        return (obj || new _DictionaryBatch()).__init(bb.readInt32(bb.position()) + bb.position(), bb);
      }
      static getSizePrefixedRootAsDictionaryBatch(bb, obj) {
        bb.setPosition(bb.position() + flatbuffers.SIZE_PREFIX_LENGTH);
        return (obj || new _DictionaryBatch()).__init(bb.readInt32(bb.position()) + bb.position(), bb);
      }
      id() {
        const offset = this.bb.__offset(this.bb_pos, 4);
        return offset ? this.bb.readInt64(this.bb_pos + offset) : BigInt("0");
      }
      data(obj) {
        const offset = this.bb.__offset(this.bb_pos, 6);
        return offset ? (obj || new record_batch_js_1.RecordBatch()).__init(this.bb.__indirect(this.bb_pos + offset), this.bb) : null;
      }
      /**
       * If isDelta is true the values in the dictionary are to be appended to a
       * dictionary with the indicated id. If isDelta is false this dictionary
       * should replace the existing dictionary.
       */
      isDelta() {
        const offset = this.bb.__offset(this.bb_pos, 8);
        return offset ? !!this.bb.readInt8(this.bb_pos + offset) : false;
      }
      static startDictionaryBatch(builder) {
        builder.startObject(3);
      }
      static addId(builder, id) {
        builder.addFieldInt64(0, id, BigInt("0"));
      }
      static addData(builder, dataOffset) {
        builder.addFieldOffset(1, dataOffset, 0);
      }
      static addIsDelta(builder, isDelta) {
        builder.addFieldInt8(2, +isDelta, 0);
      }
      static endDictionaryBatch(builder) {
        const offset = builder.endObject();
        return offset;
      }
    };
    exports2.DictionaryBatch = DictionaryBatch;
  }
});

// node_modules/apache-arrow/fb/endianness.js
var require_endianness = __commonJS({
  "node_modules/apache-arrow/fb/endianness.js"(exports2) {
    "use strict";
    Object.defineProperty(exports2, "__esModule", { value: true });
    exports2.Endianness = void 0;
    var Endianness;
    (function(Endianness2) {
      Endianness2[Endianness2["Little"] = 0] = "Little";
      Endianness2[Endianness2["Big"] = 1] = "Big";
    })(Endianness || (exports2.Endianness = Endianness = {}));
  }
});

// node_modules/apache-arrow/fb/dictionary-kind.js
var require_dictionary_kind = __commonJS({
  "node_modules/apache-arrow/fb/dictionary-kind.js"(exports2) {
    "use strict";
    Object.defineProperty(exports2, "__esModule", { value: true });
    exports2.DictionaryKind = void 0;
    var DictionaryKind;
    (function(DictionaryKind2) {
      DictionaryKind2[DictionaryKind2["DenseArray"] = 0] = "DenseArray";
    })(DictionaryKind || (exports2.DictionaryKind = DictionaryKind = {}));
  }
});

// node_modules/apache-arrow/fb/int.js
var require_int = __commonJS({
  "node_modules/apache-arrow/fb/int.js"(exports2) {
    "use strict";
    Object.defineProperty(exports2, "__esModule", { value: true });
    exports2.Int = void 0;
    var flatbuffers = (init_flatbuffers(), __toCommonJS(flatbuffers_exports));
    var Int = class _Int {
      constructor() {
        this.bb = null;
        this.bb_pos = 0;
      }
      __init(i, bb) {
        this.bb_pos = i;
        this.bb = bb;
        return this;
      }
      static getRootAsInt(bb, obj) {
        return (obj || new _Int()).__init(bb.readInt32(bb.position()) + bb.position(), bb);
      }
      static getSizePrefixedRootAsInt(bb, obj) {
        bb.setPosition(bb.position() + flatbuffers.SIZE_PREFIX_LENGTH);
        return (obj || new _Int()).__init(bb.readInt32(bb.position()) + bb.position(), bb);
      }
      bitWidth() {
        const offset = this.bb.__offset(this.bb_pos, 4);
        return offset ? this.bb.readInt32(this.bb_pos + offset) : 0;
      }
      isSigned() {
        const offset = this.bb.__offset(this.bb_pos, 6);
        return offset ? !!this.bb.readInt8(this.bb_pos + offset) : false;
      }
      static startInt(builder) {
        builder.startObject(2);
      }
      static addBitWidth(builder, bitWidth) {
        builder.addFieldInt32(0, bitWidth, 0);
      }
      static addIsSigned(builder, isSigned) {
        builder.addFieldInt8(1, +isSigned, 0);
      }
      static endInt(builder) {
        const offset = builder.endObject();
        return offset;
      }
      static createInt(builder, bitWidth, isSigned) {
        _Int.startInt(builder);
        _Int.addBitWidth(builder, bitWidth);
        _Int.addIsSigned(builder, isSigned);
        return _Int.endInt(builder);
      }
    };
    exports2.Int = Int;
  }
});

// node_modules/apache-arrow/fb/dictionary-encoding.js
var require_dictionary_encoding = __commonJS({
  "node_modules/apache-arrow/fb/dictionary-encoding.js"(exports2) {
    "use strict";
    Object.defineProperty(exports2, "__esModule", { value: true });
    exports2.DictionaryEncoding = void 0;
    var flatbuffers = (init_flatbuffers(), __toCommonJS(flatbuffers_exports));
    var dictionary_kind_js_1 = require_dictionary_kind();
    var int_js_1 = require_int();
    var DictionaryEncoding = class _DictionaryEncoding {
      constructor() {
        this.bb = null;
        this.bb_pos = 0;
      }
      __init(i, bb) {
        this.bb_pos = i;
        this.bb = bb;
        return this;
      }
      static getRootAsDictionaryEncoding(bb, obj) {
        return (obj || new _DictionaryEncoding()).__init(bb.readInt32(bb.position()) + bb.position(), bb);
      }
      static getSizePrefixedRootAsDictionaryEncoding(bb, obj) {
        bb.setPosition(bb.position() + flatbuffers.SIZE_PREFIX_LENGTH);
        return (obj || new _DictionaryEncoding()).__init(bb.readInt32(bb.position()) + bb.position(), bb);
      }
      /**
       * The known dictionary id in the application where this data is used. In
       * the file or streaming formats, the dictionary ids are found in the
       * DictionaryBatch messages
       */
      id() {
        const offset = this.bb.__offset(this.bb_pos, 4);
        return offset ? this.bb.readInt64(this.bb_pos + offset) : BigInt("0");
      }
      /**
       * The dictionary indices are constrained to be non-negative integers. If
       * this field is null, the indices must be signed int32. To maximize
       * cross-language compatibility and performance, implementations are
       * recommended to prefer signed integer types over unsigned integer types
       * and to avoid uint64 indices unless they are required by an application.
       */
      indexType(obj) {
        const offset = this.bb.__offset(this.bb_pos, 6);
        return offset ? (obj || new int_js_1.Int()).__init(this.bb.__indirect(this.bb_pos + offset), this.bb) : null;
      }
      /**
       * By default, dictionaries are not ordered, or the order does not have
       * semantic meaning. In some statistical, applications, dictionary-encoding
       * is used to represent ordered categorical data, and we provide a way to
       * preserve that metadata here
       */
      isOrdered() {
        const offset = this.bb.__offset(this.bb_pos, 8);
        return offset ? !!this.bb.readInt8(this.bb_pos + offset) : false;
      }
      dictionaryKind() {
        const offset = this.bb.__offset(this.bb_pos, 10);
        return offset ? this.bb.readInt16(this.bb_pos + offset) : dictionary_kind_js_1.DictionaryKind.DenseArray;
      }
      static startDictionaryEncoding(builder) {
        builder.startObject(4);
      }
      static addId(builder, id) {
        builder.addFieldInt64(0, id, BigInt("0"));
      }
      static addIndexType(builder, indexTypeOffset) {
        builder.addFieldOffset(1, indexTypeOffset, 0);
      }
      static addIsOrdered(builder, isOrdered) {
        builder.addFieldInt8(2, +isOrdered, 0);
      }
      static addDictionaryKind(builder, dictionaryKind) {
        builder.addFieldInt16(3, dictionaryKind, dictionary_kind_js_1.DictionaryKind.DenseArray);
      }
      static endDictionaryEncoding(builder) {
        const offset = builder.endObject();
        return offset;
      }
    };
    exports2.DictionaryEncoding = DictionaryEncoding;
  }
});

// node_modules/apache-arrow/fb/key-value.js
var require_key_value = __commonJS({
  "node_modules/apache-arrow/fb/key-value.js"(exports2) {
    "use strict";
    Object.defineProperty(exports2, "__esModule", { value: true });
    exports2.KeyValue = void 0;
    var flatbuffers = (init_flatbuffers(), __toCommonJS(flatbuffers_exports));
    var KeyValue = class _KeyValue {
      constructor() {
        this.bb = null;
        this.bb_pos = 0;
      }
      __init(i, bb) {
        this.bb_pos = i;
        this.bb = bb;
        return this;
      }
      static getRootAsKeyValue(bb, obj) {
        return (obj || new _KeyValue()).__init(bb.readInt32(bb.position()) + bb.position(), bb);
      }
      static getSizePrefixedRootAsKeyValue(bb, obj) {
        bb.setPosition(bb.position() + flatbuffers.SIZE_PREFIX_LENGTH);
        return (obj || new _KeyValue()).__init(bb.readInt32(bb.position()) + bb.position(), bb);
      }
      key(optionalEncoding) {
        const offset = this.bb.__offset(this.bb_pos, 4);
        return offset ? this.bb.__string(this.bb_pos + offset, optionalEncoding) : null;
      }
      value(optionalEncoding) {
        const offset = this.bb.__offset(this.bb_pos, 6);
        return offset ? this.bb.__string(this.bb_pos + offset, optionalEncoding) : null;
      }
      static startKeyValue(builder) {
        builder.startObject(2);
      }
      static addKey(builder, keyOffset) {
        builder.addFieldOffset(0, keyOffset, 0);
      }
      static addValue(builder, valueOffset) {
        builder.addFieldOffset(1, valueOffset, 0);
      }
      static endKeyValue(builder) {
        const offset = builder.endObject();
        return offset;
      }
      static createKeyValue(builder, keyOffset, valueOffset) {
        _KeyValue.startKeyValue(builder);
        _KeyValue.addKey(builder, keyOffset);
        _KeyValue.addValue(builder, valueOffset);
        return _KeyValue.endKeyValue(builder);
      }
    };
    exports2.KeyValue = KeyValue;
  }
});

// node_modules/apache-arrow/fb/binary.js
var require_binary = __commonJS({
  "node_modules/apache-arrow/fb/binary.js"(exports2) {
    "use strict";
    Object.defineProperty(exports2, "__esModule", { value: true });
    exports2.Binary = void 0;
    var flatbuffers = (init_flatbuffers(), __toCommonJS(flatbuffers_exports));
    var Binary = class _Binary {
      constructor() {
        this.bb = null;
        this.bb_pos = 0;
      }
      __init(i, bb) {
        this.bb_pos = i;
        this.bb = bb;
        return this;
      }
      static getRootAsBinary(bb, obj) {
        return (obj || new _Binary()).__init(bb.readInt32(bb.position()) + bb.position(), bb);
      }
      static getSizePrefixedRootAsBinary(bb, obj) {
        bb.setPosition(bb.position() + flatbuffers.SIZE_PREFIX_LENGTH);
        return (obj || new _Binary()).__init(bb.readInt32(bb.position()) + bb.position(), bb);
      }
      static startBinary(builder) {
        builder.startObject(0);
      }
      static endBinary(builder) {
        const offset = builder.endObject();
        return offset;
      }
      static createBinary(builder) {
        _Binary.startBinary(builder);
        return _Binary.endBinary(builder);
      }
    };
    exports2.Binary = Binary;
  }
});

// node_modules/apache-arrow/fb/bool.js
var require_bool = __commonJS({
  "node_modules/apache-arrow/fb/bool.js"(exports2) {
    "use strict";
    Object.defineProperty(exports2, "__esModule", { value: true });
    exports2.Bool = void 0;
    var flatbuffers = (init_flatbuffers(), __toCommonJS(flatbuffers_exports));
    var Bool = class _Bool {
      constructor() {
        this.bb = null;
        this.bb_pos = 0;
      }
      __init(i, bb) {
        this.bb_pos = i;
        this.bb = bb;
        return this;
      }
      static getRootAsBool(bb, obj) {
        return (obj || new _Bool()).__init(bb.readInt32(bb.position()) + bb.position(), bb);
      }
      static getSizePrefixedRootAsBool(bb, obj) {
        bb.setPosition(bb.position() + flatbuffers.SIZE_PREFIX_LENGTH);
        return (obj || new _Bool()).__init(bb.readInt32(bb.position()) + bb.position(), bb);
      }
      static startBool(builder) {
        builder.startObject(0);
      }
      static endBool(builder) {
        const offset = builder.endObject();
        return offset;
      }
      static createBool(builder) {
        _Bool.startBool(builder);
        return _Bool.endBool(builder);
      }
    };
    exports2.Bool = Bool;
  }
});

// node_modules/apache-arrow/fb/date.js
var require_date = __commonJS({
  "node_modules/apache-arrow/fb/date.js"(exports2) {
    "use strict";
    Object.defineProperty(exports2, "__esModule", { value: true });
    exports2.Date = void 0;
    var flatbuffers = (init_flatbuffers(), __toCommonJS(flatbuffers_exports));
    var date_unit_js_1 = require_date_unit();
    var Date2 = class _Date {
      constructor() {
        this.bb = null;
        this.bb_pos = 0;
      }
      __init(i, bb) {
        this.bb_pos = i;
        this.bb = bb;
        return this;
      }
      static getRootAsDate(bb, obj) {
        return (obj || new _Date()).__init(bb.readInt32(bb.position()) + bb.position(), bb);
      }
      static getSizePrefixedRootAsDate(bb, obj) {
        bb.setPosition(bb.position() + flatbuffers.SIZE_PREFIX_LENGTH);
        return (obj || new _Date()).__init(bb.readInt32(bb.position()) + bb.position(), bb);
      }
      unit() {
        const offset = this.bb.__offset(this.bb_pos, 4);
        return offset ? this.bb.readInt16(this.bb_pos + offset) : date_unit_js_1.DateUnit.MILLISECOND;
      }
      static startDate(builder) {
        builder.startObject(1);
      }
      static addUnit(builder, unit) {
        builder.addFieldInt16(0, unit, date_unit_js_1.DateUnit.MILLISECOND);
      }
      static endDate(builder) {
        const offset = builder.endObject();
        return offset;
      }
      static createDate(builder, unit) {
        _Date.startDate(builder);
        _Date.addUnit(builder, unit);
        return _Date.endDate(builder);
      }
    };
    exports2.Date = Date2;
  }
});

// node_modules/apache-arrow/fb/decimal.js
var require_decimal = __commonJS({
  "node_modules/apache-arrow/fb/decimal.js"(exports2) {
    "use strict";
    Object.defineProperty(exports2, "__esModule", { value: true });
    exports2.Decimal = void 0;
    var flatbuffers = (init_flatbuffers(), __toCommonJS(flatbuffers_exports));
    var Decimal = class _Decimal {
      constructor() {
        this.bb = null;
        this.bb_pos = 0;
      }
      __init(i, bb) {
        this.bb_pos = i;
        this.bb = bb;
        return this;
      }
      static getRootAsDecimal(bb, obj) {
        return (obj || new _Decimal()).__init(bb.readInt32(bb.position()) + bb.position(), bb);
      }
      static getSizePrefixedRootAsDecimal(bb, obj) {
        bb.setPosition(bb.position() + flatbuffers.SIZE_PREFIX_LENGTH);
        return (obj || new _Decimal()).__init(bb.readInt32(bb.position()) + bb.position(), bb);
      }
      /**
       * Total number of decimal digits
       */
      precision() {
        const offset = this.bb.__offset(this.bb_pos, 4);
        return offset ? this.bb.readInt32(this.bb_pos + offset) : 0;
      }
      /**
       * Number of digits after the decimal point "."
       */
      scale() {
        const offset = this.bb.__offset(this.bb_pos, 6);
        return offset ? this.bb.readInt32(this.bb_pos + offset) : 0;
      }
      /**
       * Number of bits per value. The only accepted widths are 128 and 256.
       * We use bitWidth for consistency with Int::bitWidth.
       */
      bitWidth() {
        const offset = this.bb.__offset(this.bb_pos, 8);
        return offset ? this.bb.readInt32(this.bb_pos + offset) : 128;
      }
      static startDecimal(builder) {
        builder.startObject(3);
      }
      static addPrecision(builder, precision) {
        builder.addFieldInt32(0, precision, 0);
      }
      static addScale(builder, scale) {
        builder.addFieldInt32(1, scale, 0);
      }
      static addBitWidth(builder, bitWidth) {
        builder.addFieldInt32(2, bitWidth, 128);
      }
      static endDecimal(builder) {
        const offset = builder.endObject();
        return offset;
      }
      static createDecimal(builder, precision, scale, bitWidth) {
        _Decimal.startDecimal(builder);
        _Decimal.addPrecision(builder, precision);
        _Decimal.addScale(builder, scale);
        _Decimal.addBitWidth(builder, bitWidth);
        return _Decimal.endDecimal(builder);
      }
    };
    exports2.Decimal = Decimal;
  }
});

// node_modules/apache-arrow/fb/duration.js
var require_duration = __commonJS({
  "node_modules/apache-arrow/fb/duration.js"(exports2) {
    "use strict";
    Object.defineProperty(exports2, "__esModule", { value: true });
    exports2.Duration = void 0;
    var flatbuffers = (init_flatbuffers(), __toCommonJS(flatbuffers_exports));
    var time_unit_js_1 = require_time_unit();
    var Duration = class _Duration {
      constructor() {
        this.bb = null;
        this.bb_pos = 0;
      }
      __init(i, bb) {
        this.bb_pos = i;
        this.bb = bb;
        return this;
      }
      static getRootAsDuration(bb, obj) {
        return (obj || new _Duration()).__init(bb.readInt32(bb.position()) + bb.position(), bb);
      }
      static getSizePrefixedRootAsDuration(bb, obj) {
        bb.setPosition(bb.position() + flatbuffers.SIZE_PREFIX_LENGTH);
        return (obj || new _Duration()).__init(bb.readInt32(bb.position()) + bb.position(), bb);
      }
      unit() {
        const offset = this.bb.__offset(this.bb_pos, 4);
        return offset ? this.bb.readInt16(this.bb_pos + offset) : time_unit_js_1.TimeUnit.MILLISECOND;
      }
      static startDuration(builder) {
        builder.startObject(1);
      }
      static addUnit(builder, unit) {
        builder.addFieldInt16(0, unit, time_unit_js_1.TimeUnit.MILLISECOND);
      }
      static endDuration(builder) {
        const offset = builder.endObject();
        return offset;
      }
      static createDuration(builder, unit) {
        _Duration.startDuration(builder);
        _Duration.addUnit(builder, unit);
        return _Duration.endDuration(builder);
      }
    };
    exports2.Duration = Duration;
  }
});

// node_modules/apache-arrow/fb/fixed-size-binary.js
var require_fixed_size_binary = __commonJS({
  "node_modules/apache-arrow/fb/fixed-size-binary.js"(exports2) {
    "use strict";
    Object.defineProperty(exports2, "__esModule", { value: true });
    exports2.FixedSizeBinary = void 0;
    var flatbuffers = (init_flatbuffers(), __toCommonJS(flatbuffers_exports));
    var FixedSizeBinary = class _FixedSizeBinary {
      constructor() {
        this.bb = null;
        this.bb_pos = 0;
      }
      __init(i, bb) {
        this.bb_pos = i;
        this.bb = bb;
        return this;
      }
      static getRootAsFixedSizeBinary(bb, obj) {
        return (obj || new _FixedSizeBinary()).__init(bb.readInt32(bb.position()) + bb.position(), bb);
      }
      static getSizePrefixedRootAsFixedSizeBinary(bb, obj) {
        bb.setPosition(bb.position() + flatbuffers.SIZE_PREFIX_LENGTH);
        return (obj || new _FixedSizeBinary()).__init(bb.readInt32(bb.position()) + bb.position(), bb);
      }
      /**
       * Number of bytes per value
       */
      byteWidth() {
        const offset = this.bb.__offset(this.bb_pos, 4);
        return offset ? this.bb.readInt32(this.bb_pos + offset) : 0;
      }
      static startFixedSizeBinary(builder) {
        builder.startObject(1);
      }
      static addByteWidth(builder, byteWidth) {
        builder.addFieldInt32(0, byteWidth, 0);
      }
      static endFixedSizeBinary(builder) {
        const offset = builder.endObject();
        return offset;
      }
      static createFixedSizeBinary(builder, byteWidth) {
        _FixedSizeBinary.startFixedSizeBinary(builder);
        _FixedSizeBinary.addByteWidth(builder, byteWidth);
        return _FixedSizeBinary.endFixedSizeBinary(builder);
      }
    };
    exports2.FixedSizeBinary = FixedSizeBinary;
  }
});

// node_modules/apache-arrow/fb/fixed-size-list.js
var require_fixed_size_list = __commonJS({
  "node_modules/apache-arrow/fb/fixed-size-list.js"(exports2) {
    "use strict";
    Object.defineProperty(exports2, "__esModule", { value: true });
    exports2.FixedSizeList = void 0;
    var flatbuffers = (init_flatbuffers(), __toCommonJS(flatbuffers_exports));
    var FixedSizeList = class _FixedSizeList {
      constructor() {
        this.bb = null;
        this.bb_pos = 0;
      }
      __init(i, bb) {
        this.bb_pos = i;
        this.bb = bb;
        return this;
      }
      static getRootAsFixedSizeList(bb, obj) {
        return (obj || new _FixedSizeList()).__init(bb.readInt32(bb.position()) + bb.position(), bb);
      }
      static getSizePrefixedRootAsFixedSizeList(bb, obj) {
        bb.setPosition(bb.position() + flatbuffers.SIZE_PREFIX_LENGTH);
        return (obj || new _FixedSizeList()).__init(bb.readInt32(bb.position()) + bb.position(), bb);
      }
      /**
       * Number of list items per value
       */
      listSize() {
        const offset = this.bb.__offset(this.bb_pos, 4);
        return offset ? this.bb.readInt32(this.bb_pos + offset) : 0;
      }
      static startFixedSizeList(builder) {
        builder.startObject(1);
      }
      static addListSize(builder, listSize) {
        builder.addFieldInt32(0, listSize, 0);
      }
      static endFixedSizeList(builder) {
        const offset = builder.endObject();
        return offset;
      }
      static createFixedSizeList(builder, listSize) {
        _FixedSizeList.startFixedSizeList(builder);
        _FixedSizeList.addListSize(builder, listSize);
        return _FixedSizeList.endFixedSizeList(builder);
      }
    };
    exports2.FixedSizeList = FixedSizeList;
  }
});

// node_modules/apache-arrow/fb/floating-point.js
var require_floating_point = __commonJS({
  "node_modules/apache-arrow/fb/floating-point.js"(exports2) {
    "use strict";
    Object.defineProperty(exports2, "__esModule", { value: true });
    exports2.FloatingPoint = void 0;
    var flatbuffers = (init_flatbuffers(), __toCommonJS(flatbuffers_exports));
    var precision_js_1 = require_precision();
    var FloatingPoint = class _FloatingPoint {
      constructor() {
        this.bb = null;
        this.bb_pos = 0;
      }
      __init(i, bb) {
        this.bb_pos = i;
        this.bb = bb;
        return this;
      }
      static getRootAsFloatingPoint(bb, obj) {
        return (obj || new _FloatingPoint()).__init(bb.readInt32(bb.position()) + bb.position(), bb);
      }
      static getSizePrefixedRootAsFloatingPoint(bb, obj) {
        bb.setPosition(bb.position() + flatbuffers.SIZE_PREFIX_LENGTH);
        return (obj || new _FloatingPoint()).__init(bb.readInt32(bb.position()) + bb.position(), bb);
      }
      precision() {
        const offset = this.bb.__offset(this.bb_pos, 4);
        return offset ? this.bb.readInt16(this.bb_pos + offset) : precision_js_1.Precision.HALF;
      }
      static startFloatingPoint(builder) {
        builder.startObject(1);
      }
      static addPrecision(builder, precision) {
        builder.addFieldInt16(0, precision, precision_js_1.Precision.HALF);
      }
      static endFloatingPoint(builder) {
        const offset = builder.endObject();
        return offset;
      }
      static createFloatingPoint(builder, precision) {
        _FloatingPoint.startFloatingPoint(builder);
        _FloatingPoint.addPrecision(builder, precision);
        return _FloatingPoint.endFloatingPoint(builder);
      }
    };
    exports2.FloatingPoint = FloatingPoint;
  }
});

// node_modules/apache-arrow/fb/interval.js
var require_interval = __commonJS({
  "node_modules/apache-arrow/fb/interval.js"(exports2) {
    "use strict";
    Object.defineProperty(exports2, "__esModule", { value: true });
    exports2.Interval = void 0;
    var flatbuffers = (init_flatbuffers(), __toCommonJS(flatbuffers_exports));
    var interval_unit_js_1 = require_interval_unit();
    var Interval = class _Interval {
      constructor() {
        this.bb = null;
        this.bb_pos = 0;
      }
      __init(i, bb) {
        this.bb_pos = i;
        this.bb = bb;
        return this;
      }
      static getRootAsInterval(bb, obj) {
        return (obj || new _Interval()).__init(bb.readInt32(bb.position()) + bb.position(), bb);
      }
      static getSizePrefixedRootAsInterval(bb, obj) {
        bb.setPosition(bb.position() + flatbuffers.SIZE_PREFIX_LENGTH);
        return (obj || new _Interval()).__init(bb.readInt32(bb.position()) + bb.position(), bb);
      }
      unit() {
        const offset = this.bb.__offset(this.bb_pos, 4);
        return offset ? this.bb.readInt16(this.bb_pos + offset) : interval_unit_js_1.IntervalUnit.YEAR_MONTH;
      }
      static startInterval(builder) {
        builder.startObject(1);
      }
      static addUnit(builder, unit) {
        builder.addFieldInt16(0, unit, interval_unit_js_1.IntervalUnit.YEAR_MONTH);
      }
      static endInterval(builder) {
        const offset = builder.endObject();
        return offset;
      }
      static createInterval(builder, unit) {
        _Interval.startInterval(builder);
        _Interval.addUnit(builder, unit);
        return _Interval.endInterval(builder);
      }
    };
    exports2.Interval = Interval;
  }
});

// node_modules/apache-arrow/fb/large-binary.js
var require_large_binary = __commonJS({
  "node_modules/apache-arrow/fb/large-binary.js"(exports2) {
    "use strict";
    Object.defineProperty(exports2, "__esModule", { value: true });
    exports2.LargeBinary = void 0;
    var flatbuffers = (init_flatbuffers(), __toCommonJS(flatbuffers_exports));
    var LargeBinary = class _LargeBinary {
      constructor() {
        this.bb = null;
        this.bb_pos = 0;
      }
      __init(i, bb) {
        this.bb_pos = i;
        this.bb = bb;
        return this;
      }
      static getRootAsLargeBinary(bb, obj) {
        return (obj || new _LargeBinary()).__init(bb.readInt32(bb.position()) + bb.position(), bb);
      }
      static getSizePrefixedRootAsLargeBinary(bb, obj) {
        bb.setPosition(bb.position() + flatbuffers.SIZE_PREFIX_LENGTH);
        return (obj || new _LargeBinary()).__init(bb.readInt32(bb.position()) + bb.position(), bb);
      }
      static startLargeBinary(builder) {
        builder.startObject(0);
      }
      static endLargeBinary(builder) {
        const offset = builder.endObject();
        return offset;
      }
      static createLargeBinary(builder) {
        _LargeBinary.startLargeBinary(builder);
        return _LargeBinary.endLargeBinary(builder);
      }
    };
    exports2.LargeBinary = LargeBinary;
  }
});

// node_modules/apache-arrow/fb/large-list.js
var require_large_list = __commonJS({
  "node_modules/apache-arrow/fb/large-list.js"(exports2) {
    "use strict";
    Object.defineProperty(exports2, "__esModule", { value: true });
    exports2.LargeList = void 0;
    var flatbuffers = (init_flatbuffers(), __toCommonJS(flatbuffers_exports));
    var LargeList = class _LargeList {
      constructor() {
        this.bb = null;
        this.bb_pos = 0;
      }
      __init(i, bb) {
        this.bb_pos = i;
        this.bb = bb;
        return this;
      }
      static getRootAsLargeList(bb, obj) {
        return (obj || new _LargeList()).__init(bb.readInt32(bb.position()) + bb.position(), bb);
      }
      static getSizePrefixedRootAsLargeList(bb, obj) {
        bb.setPosition(bb.position() + flatbuffers.SIZE_PREFIX_LENGTH);
        return (obj || new _LargeList()).__init(bb.readInt32(bb.position()) + bb.position(), bb);
      }
      static startLargeList(builder) {
        builder.startObject(0);
      }
      static endLargeList(builder) {
        const offset = builder.endObject();
        return offset;
      }
      static createLargeList(builder) {
        _LargeList.startLargeList(builder);
        return _LargeList.endLargeList(builder);
      }
    };
    exports2.LargeList = LargeList;
  }
});

// node_modules/apache-arrow/fb/large-utf8.js
var require_large_utf8 = __commonJS({
  "node_modules/apache-arrow/fb/large-utf8.js"(exports2) {
    "use strict";
    Object.defineProperty(exports2, "__esModule", { value: true });
    exports2.LargeUtf8 = void 0;
    var flatbuffers = (init_flatbuffers(), __toCommonJS(flatbuffers_exports));
    var LargeUtf8 = class _LargeUtf8 {
      constructor() {
        this.bb = null;
        this.bb_pos = 0;
      }
      __init(i, bb) {
        this.bb_pos = i;
        this.bb = bb;
        return this;
      }
      static getRootAsLargeUtf8(bb, obj) {
        return (obj || new _LargeUtf8()).__init(bb.readInt32(bb.position()) + bb.position(), bb);
      }
      static getSizePrefixedRootAsLargeUtf8(bb, obj) {
        bb.setPosition(bb.position() + flatbuffers.SIZE_PREFIX_LENGTH);
        return (obj || new _LargeUtf8()).__init(bb.readInt32(bb.position()) + bb.position(), bb);
      }
      static startLargeUtf8(builder) {
        builder.startObject(0);
      }
      static endLargeUtf8(builder) {
        const offset = builder.endObject();
        return offset;
      }
      static createLargeUtf8(builder) {
        _LargeUtf8.startLargeUtf8(builder);
        return _LargeUtf8.endLargeUtf8(builder);
      }
    };
    exports2.LargeUtf8 = LargeUtf8;
  }
});

// node_modules/apache-arrow/fb/list.js
var require_list = __commonJS({
  "node_modules/apache-arrow/fb/list.js"(exports2) {
    "use strict";
    Object.defineProperty(exports2, "__esModule", { value: true });
    exports2.List = void 0;
    var flatbuffers = (init_flatbuffers(), __toCommonJS(flatbuffers_exports));
    var List = class _List {
      constructor() {
        this.bb = null;
        this.bb_pos = 0;
      }
      __init(i, bb) {
        this.bb_pos = i;
        this.bb = bb;
        return this;
      }
      static getRootAsList(bb, obj) {
        return (obj || new _List()).__init(bb.readInt32(bb.position()) + bb.position(), bb);
      }
      static getSizePrefixedRootAsList(bb, obj) {
        bb.setPosition(bb.position() + flatbuffers.SIZE_PREFIX_LENGTH);
        return (obj || new _List()).__init(bb.readInt32(bb.position()) + bb.position(), bb);
      }
      static startList(builder) {
        builder.startObject(0);
      }
      static endList(builder) {
        const offset = builder.endObject();
        return offset;
      }
      static createList(builder) {
        _List.startList(builder);
        return _List.endList(builder);
      }
    };
    exports2.List = List;
  }
});

// node_modules/apache-arrow/fb/map.js
var require_map = __commonJS({
  "node_modules/apache-arrow/fb/map.js"(exports2) {
    "use strict";
    Object.defineProperty(exports2, "__esModule", { value: true });
    exports2.Map = void 0;
    var flatbuffers = (init_flatbuffers(), __toCommonJS(flatbuffers_exports));
    var Map2 = class _Map {
      constructor() {
        this.bb = null;
        this.bb_pos = 0;
      }
      __init(i, bb) {
        this.bb_pos = i;
        this.bb = bb;
        return this;
      }
      static getRootAsMap(bb, obj) {
        return (obj || new _Map()).__init(bb.readInt32(bb.position()) + bb.position(), bb);
      }
      static getSizePrefixedRootAsMap(bb, obj) {
        bb.setPosition(bb.position() + flatbuffers.SIZE_PREFIX_LENGTH);
        return (obj || new _Map()).__init(bb.readInt32(bb.position()) + bb.position(), bb);
      }
      /**
       * Set to true if the keys within each value are sorted
       */
      keysSorted() {
        const offset = this.bb.__offset(this.bb_pos, 4);
        return offset ? !!this.bb.readInt8(this.bb_pos + offset) : false;
      }
      static startMap(builder) {
        builder.startObject(1);
      }
      static addKeysSorted(builder, keysSorted) {
        builder.addFieldInt8(0, +keysSorted, 0);
      }
      static endMap(builder) {
        const offset = builder.endObject();
        return offset;
      }
      static createMap(builder, keysSorted) {
        _Map.startMap(builder);
        _Map.addKeysSorted(builder, keysSorted);
        return _Map.endMap(builder);
      }
    };
    exports2.Map = Map2;
  }
});

// node_modules/apache-arrow/fb/null.js
var require_null = __commonJS({
  "node_modules/apache-arrow/fb/null.js"(exports2) {
    "use strict";
    Object.defineProperty(exports2, "__esModule", { value: true });
    exports2.Null = void 0;
    var flatbuffers = (init_flatbuffers(), __toCommonJS(flatbuffers_exports));
    var Null = class _Null {
      constructor() {
        this.bb = null;
        this.bb_pos = 0;
      }
      __init(i, bb) {
        this.bb_pos = i;
        this.bb = bb;
        return this;
      }
      static getRootAsNull(bb, obj) {
        return (obj || new _Null()).__init(bb.readInt32(bb.position()) + bb.position(), bb);
      }
      static getSizePrefixedRootAsNull(bb, obj) {
        bb.setPosition(bb.position() + flatbuffers.SIZE_PREFIX_LENGTH);
        return (obj || new _Null()).__init(bb.readInt32(bb.position()) + bb.position(), bb);
      }
      static startNull(builder) {
        builder.startObject(0);
      }
      static endNull(builder) {
        const offset = builder.endObject();
        return offset;
      }
      static createNull(builder) {
        _Null.startNull(builder);
        return _Null.endNull(builder);
      }
    };
    exports2.Null = Null;
  }
});

// node_modules/apache-arrow/fb/run-end-encoded.js
var require_run_end_encoded = __commonJS({
  "node_modules/apache-arrow/fb/run-end-encoded.js"(exports2) {
    "use strict";
    Object.defineProperty(exports2, "__esModule", { value: true });
    exports2.RunEndEncoded = void 0;
    var flatbuffers = (init_flatbuffers(), __toCommonJS(flatbuffers_exports));
    var RunEndEncoded = class _RunEndEncoded {
      constructor() {
        this.bb = null;
        this.bb_pos = 0;
      }
      __init(i, bb) {
        this.bb_pos = i;
        this.bb = bb;
        return this;
      }
      static getRootAsRunEndEncoded(bb, obj) {
        return (obj || new _RunEndEncoded()).__init(bb.readInt32(bb.position()) + bb.position(), bb);
      }
      static getSizePrefixedRootAsRunEndEncoded(bb, obj) {
        bb.setPosition(bb.position() + flatbuffers.SIZE_PREFIX_LENGTH);
        return (obj || new _RunEndEncoded()).__init(bb.readInt32(bb.position()) + bb.position(), bb);
      }
      static startRunEndEncoded(builder) {
        builder.startObject(0);
      }
      static endRunEndEncoded(builder) {
        const offset = builder.endObject();
        return offset;
      }
      static createRunEndEncoded(builder) {
        _RunEndEncoded.startRunEndEncoded(builder);
        return _RunEndEncoded.endRunEndEncoded(builder);
      }
    };
    exports2.RunEndEncoded = RunEndEncoded;
  }
});

// node_modules/apache-arrow/fb/struct-.js
var require_struct = __commonJS({
  "node_modules/apache-arrow/fb/struct-.js"(exports2) {
    "use strict";
    Object.defineProperty(exports2, "__esModule", { value: true });
    exports2.Struct_ = void 0;
    var flatbuffers = (init_flatbuffers(), __toCommonJS(flatbuffers_exports));
    var Struct_ = class _Struct_ {
      constructor() {
        this.bb = null;
        this.bb_pos = 0;
      }
      __init(i, bb) {
        this.bb_pos = i;
        this.bb = bb;
        return this;
      }
      static getRootAsStruct_(bb, obj) {
        return (obj || new _Struct_()).__init(bb.readInt32(bb.position()) + bb.position(), bb);
      }
      static getSizePrefixedRootAsStruct_(bb, obj) {
        bb.setPosition(bb.position() + flatbuffers.SIZE_PREFIX_LENGTH);
        return (obj || new _Struct_()).__init(bb.readInt32(bb.position()) + bb.position(), bb);
      }
      static startStruct_(builder) {
        builder.startObject(0);
      }
      static endStruct_(builder) {
        const offset = builder.endObject();
        return offset;
      }
      static createStruct_(builder) {
        _Struct_.startStruct_(builder);
        return _Struct_.endStruct_(builder);
      }
    };
    exports2.Struct_ = Struct_;
  }
});

// node_modules/apache-arrow/fb/time.js
var require_time = __commonJS({
  "node_modules/apache-arrow/fb/time.js"(exports2) {
    "use strict";
    Object.defineProperty(exports2, "__esModule", { value: true });
    exports2.Time = void 0;
    var flatbuffers = (init_flatbuffers(), __toCommonJS(flatbuffers_exports));
    var time_unit_js_1 = require_time_unit();
    var Time = class _Time {
      constructor() {
        this.bb = null;
        this.bb_pos = 0;
      }
      __init(i, bb) {
        this.bb_pos = i;
        this.bb = bb;
        return this;
      }
      static getRootAsTime(bb, obj) {
        return (obj || new _Time()).__init(bb.readInt32(bb.position()) + bb.position(), bb);
      }
      static getSizePrefixedRootAsTime(bb, obj) {
        bb.setPosition(bb.position() + flatbuffers.SIZE_PREFIX_LENGTH);
        return (obj || new _Time()).__init(bb.readInt32(bb.position()) + bb.position(), bb);
      }
      unit() {
        const offset = this.bb.__offset(this.bb_pos, 4);
        return offset ? this.bb.readInt16(this.bb_pos + offset) : time_unit_js_1.TimeUnit.MILLISECOND;
      }
      bitWidth() {
        const offset = this.bb.__offset(this.bb_pos, 6);
        return offset ? this.bb.readInt32(this.bb_pos + offset) : 32;
      }
      static startTime(builder) {
        builder.startObject(2);
      }
      static addUnit(builder, unit) {
        builder.addFieldInt16(0, unit, time_unit_js_1.TimeUnit.MILLISECOND);
      }
      static addBitWidth(builder, bitWidth) {
        builder.addFieldInt32(1, bitWidth, 32);
      }
      static endTime(builder) {
        const offset = builder.endObject();
        return offset;
      }
      static createTime(builder, unit, bitWidth) {
        _Time.startTime(builder);
        _Time.addUnit(builder, unit);
        _Time.addBitWidth(builder, bitWidth);
        return _Time.endTime(builder);
      }
    };
    exports2.Time = Time;
  }
});

// node_modules/apache-arrow/fb/timestamp.js
var require_timestamp = __commonJS({
  "node_modules/apache-arrow/fb/timestamp.js"(exports2) {
    "use strict";
    Object.defineProperty(exports2, "__esModule", { value: true });
    exports2.Timestamp = void 0;
    var flatbuffers = (init_flatbuffers(), __toCommonJS(flatbuffers_exports));
    var time_unit_js_1 = require_time_unit();
    var Timestamp = class _Timestamp {
      constructor() {
        this.bb = null;
        this.bb_pos = 0;
      }
      __init(i, bb) {
        this.bb_pos = i;
        this.bb = bb;
        return this;
      }
      static getRootAsTimestamp(bb, obj) {
        return (obj || new _Timestamp()).__init(bb.readInt32(bb.position()) + bb.position(), bb);
      }
      static getSizePrefixedRootAsTimestamp(bb, obj) {
        bb.setPosition(bb.position() + flatbuffers.SIZE_PREFIX_LENGTH);
        return (obj || new _Timestamp()).__init(bb.readInt32(bb.position()) + bb.position(), bb);
      }
      unit() {
        const offset = this.bb.__offset(this.bb_pos, 4);
        return offset ? this.bb.readInt16(this.bb_pos + offset) : time_unit_js_1.TimeUnit.SECOND;
      }
      timezone(optionalEncoding) {
        const offset = this.bb.__offset(this.bb_pos, 6);
        return offset ? this.bb.__string(this.bb_pos + offset, optionalEncoding) : null;
      }
      static startTimestamp(builder) {
        builder.startObject(2);
      }
      static addUnit(builder, unit) {
        builder.addFieldInt16(0, unit, time_unit_js_1.TimeUnit.SECOND);
      }
      static addTimezone(builder, timezoneOffset) {
        builder.addFieldOffset(1, timezoneOffset, 0);
      }
      static endTimestamp(builder) {
        const offset = builder.endObject();
        return offset;
      }
      static createTimestamp(builder, unit, timezoneOffset) {
        _Timestamp.startTimestamp(builder);
        _Timestamp.addUnit(builder, unit);
        _Timestamp.addTimezone(builder, timezoneOffset);
        return _Timestamp.endTimestamp(builder);
      }
    };
    exports2.Timestamp = Timestamp;
  }
});

// node_modules/apache-arrow/fb/union.js
var require_union = __commonJS({
  "node_modules/apache-arrow/fb/union.js"(exports2) {
    "use strict";
    Object.defineProperty(exports2, "__esModule", { value: true });
    exports2.Union = void 0;
    var flatbuffers = (init_flatbuffers(), __toCommonJS(flatbuffers_exports));
    var union_mode_js_1 = require_union_mode();
    var Union = class _Union {
      constructor() {
        this.bb = null;
        this.bb_pos = 0;
      }
      __init(i, bb) {
        this.bb_pos = i;
        this.bb = bb;
        return this;
      }
      static getRootAsUnion(bb, obj) {
        return (obj || new _Union()).__init(bb.readInt32(bb.position()) + bb.position(), bb);
      }
      static getSizePrefixedRootAsUnion(bb, obj) {
        bb.setPosition(bb.position() + flatbuffers.SIZE_PREFIX_LENGTH);
        return (obj || new _Union()).__init(bb.readInt32(bb.position()) + bb.position(), bb);
      }
      mode() {
        const offset = this.bb.__offset(this.bb_pos, 4);
        return offset ? this.bb.readInt16(this.bb_pos + offset) : union_mode_js_1.UnionMode.Sparse;
      }
      typeIds(index) {
        const offset = this.bb.__offset(this.bb_pos, 6);
        return offset ? this.bb.readInt32(this.bb.__vector(this.bb_pos + offset) + index * 4) : 0;
      }
      typeIdsLength() {
        const offset = this.bb.__offset(this.bb_pos, 6);
        return offset ? this.bb.__vector_len(this.bb_pos + offset) : 0;
      }
      typeIdsArray() {
        const offset = this.bb.__offset(this.bb_pos, 6);
        return offset ? new Int32Array(this.bb.bytes().buffer, this.bb.bytes().byteOffset + this.bb.__vector(this.bb_pos + offset), this.bb.__vector_len(this.bb_pos + offset)) : null;
      }
      static startUnion(builder) {
        builder.startObject(2);
      }
      static addMode(builder, mode) {
        builder.addFieldInt16(0, mode, union_mode_js_1.UnionMode.Sparse);
      }
      static addTypeIds(builder, typeIdsOffset) {
        builder.addFieldOffset(1, typeIdsOffset, 0);
      }
      static createTypeIdsVector(builder, data) {
        builder.startVector(4, data.length, 4);
        for (let i = data.length - 1; i >= 0; i--) {
          builder.addInt32(data[i]);
        }
        return builder.endVector();
      }
      static startTypeIdsVector(builder, numElems) {
        builder.startVector(4, numElems, 4);
      }
      static endUnion(builder) {
        const offset = builder.endObject();
        return offset;
      }
      static createUnion(builder, mode, typeIdsOffset) {
        _Union.startUnion(builder);
        _Union.addMode(builder, mode);
        _Union.addTypeIds(builder, typeIdsOffset);
        return _Union.endUnion(builder);
      }
    };
    exports2.Union = Union;
  }
});

// node_modules/apache-arrow/fb/utf8.js
var require_utf82 = __commonJS({
  "node_modules/apache-arrow/fb/utf8.js"(exports2) {
    "use strict";
    Object.defineProperty(exports2, "__esModule", { value: true });
    exports2.Utf8 = void 0;
    var flatbuffers = (init_flatbuffers(), __toCommonJS(flatbuffers_exports));
    var Utf8 = class _Utf8 {
      constructor() {
        this.bb = null;
        this.bb_pos = 0;
      }
      __init(i, bb) {
        this.bb_pos = i;
        this.bb = bb;
        return this;
      }
      static getRootAsUtf8(bb, obj) {
        return (obj || new _Utf8()).__init(bb.readInt32(bb.position()) + bb.position(), bb);
      }
      static getSizePrefixedRootAsUtf8(bb, obj) {
        bb.setPosition(bb.position() + flatbuffers.SIZE_PREFIX_LENGTH);
        return (obj || new _Utf8()).__init(bb.readInt32(bb.position()) + bb.position(), bb);
      }
      static startUtf8(builder) {
        builder.startObject(0);
      }
      static endUtf8(builder) {
        const offset = builder.endObject();
        return offset;
      }
      static createUtf8(builder) {
        _Utf8.startUtf8(builder);
        return _Utf8.endUtf8(builder);
      }
    };
    exports2.Utf8 = Utf8;
  }
});

// node_modules/apache-arrow/fb/type.js
var require_type = __commonJS({
  "node_modules/apache-arrow/fb/type.js"(exports2) {
    "use strict";
    Object.defineProperty(exports2, "__esModule", { value: true });
    exports2.unionListToType = exports2.unionToType = exports2.Type = void 0;
    var binary_js_1 = require_binary();
    var bool_js_1 = require_bool();
    var date_js_1 = require_date();
    var decimal_js_1 = require_decimal();
    var duration_js_1 = require_duration();
    var fixed_size_binary_js_1 = require_fixed_size_binary();
    var fixed_size_list_js_1 = require_fixed_size_list();
    var floating_point_js_1 = require_floating_point();
    var int_js_1 = require_int();
    var interval_js_1 = require_interval();
    var large_binary_js_1 = require_large_binary();
    var large_list_js_1 = require_large_list();
    var large_utf8_js_1 = require_large_utf8();
    var list_js_1 = require_list();
    var map_js_1 = require_map();
    var null_js_1 = require_null();
    var run_end_encoded_js_1 = require_run_end_encoded();
    var struct__js_1 = require_struct();
    var time_js_1 = require_time();
    var timestamp_js_1 = require_timestamp();
    var union_js_1 = require_union();
    var utf8_js_1 = require_utf82();
    var Type;
    (function(Type2) {
      Type2[Type2["NONE"] = 0] = "NONE";
      Type2[Type2["Null"] = 1] = "Null";
      Type2[Type2["Int"] = 2] = "Int";
      Type2[Type2["FloatingPoint"] = 3] = "FloatingPoint";
      Type2[Type2["Binary"] = 4] = "Binary";
      Type2[Type2["Utf8"] = 5] = "Utf8";
      Type2[Type2["Bool"] = 6] = "Bool";
      Type2[Type2["Decimal"] = 7] = "Decimal";
      Type2[Type2["Date"] = 8] = "Date";
      Type2[Type2["Time"] = 9] = "Time";
      Type2[Type2["Timestamp"] = 10] = "Timestamp";
      Type2[Type2["Interval"] = 11] = "Interval";
      Type2[Type2["List"] = 12] = "List";
      Type2[Type2["Struct_"] = 13] = "Struct_";
      Type2[Type2["Union"] = 14] = "Union";
      Type2[Type2["FixedSizeBinary"] = 15] = "FixedSizeBinary";
      Type2[Type2["FixedSizeList"] = 16] = "FixedSizeList";
      Type2[Type2["Map"] = 17] = "Map";
      Type2[Type2["Duration"] = 18] = "Duration";
      Type2[Type2["LargeBinary"] = 19] = "LargeBinary";
      Type2[Type2["LargeUtf8"] = 20] = "LargeUtf8";
      Type2[Type2["LargeList"] = 21] = "LargeList";
      Type2[Type2["RunEndEncoded"] = 22] = "RunEndEncoded";
    })(Type || (exports2.Type = Type = {}));
    function unionToType(type, accessor) {
      switch (Type[type]) {
        case "NONE":
          return null;
        case "Null":
          return accessor(new null_js_1.Null());
        case "Int":
          return accessor(new int_js_1.Int());
        case "FloatingPoint":
          return accessor(new floating_point_js_1.FloatingPoint());
        case "Binary":
          return accessor(new binary_js_1.Binary());
        case "Utf8":
          return accessor(new utf8_js_1.Utf8());
        case "Bool":
          return accessor(new bool_js_1.Bool());
        case "Decimal":
          return accessor(new decimal_js_1.Decimal());
        case "Date":
          return accessor(new date_js_1.Date());
        case "Time":
          return accessor(new time_js_1.Time());
        case "Timestamp":
          return accessor(new timestamp_js_1.Timestamp());
        case "Interval":
          return accessor(new interval_js_1.Interval());
        case "List":
          return accessor(new list_js_1.List());
        case "Struct_":
          return accessor(new struct__js_1.Struct_());
        case "Union":
          return accessor(new union_js_1.Union());
        case "FixedSizeBinary":
          return accessor(new fixed_size_binary_js_1.FixedSizeBinary());
        case "FixedSizeList":
          return accessor(new fixed_size_list_js_1.FixedSizeList());
        case "Map":
          return accessor(new map_js_1.Map());
        case "Duration":
          return accessor(new duration_js_1.Duration());
        case "LargeBinary":
          return accessor(new large_binary_js_1.LargeBinary());
        case "LargeUtf8":
          return accessor(new large_utf8_js_1.LargeUtf8());
        case "LargeList":
          return accessor(new large_list_js_1.LargeList());
        case "RunEndEncoded":
          return accessor(new run_end_encoded_js_1.RunEndEncoded());
        default:
          return null;
      }
    }
    exports2.unionToType = unionToType;
    function unionListToType(type, accessor, index) {
      switch (Type[type]) {
        case "NONE":
          return null;
        case "Null":
          return accessor(index, new null_js_1.Null());
        case "Int":
          return accessor(index, new int_js_1.Int());
        case "FloatingPoint":
          return accessor(index, new floating_point_js_1.FloatingPoint());
        case "Binary":
          return accessor(index, new binary_js_1.Binary());
        case "Utf8":
          return accessor(index, new utf8_js_1.Utf8());
        case "Bool":
          return accessor(index, new bool_js_1.Bool());
        case "Decimal":
          return accessor(index, new decimal_js_1.Decimal());
        case "Date":
          return accessor(index, new date_js_1.Date());
        case "Time":
          return accessor(index, new time_js_1.Time());
        case "Timestamp":
          return accessor(index, new timestamp_js_1.Timestamp());
        case "Interval":
          return accessor(index, new interval_js_1.Interval());
        case "List":
          return accessor(index, new list_js_1.List());
        case "Struct_":
          return accessor(index, new struct__js_1.Struct_());
        case "Union":
          return accessor(index, new union_js_1.Union());
        case "FixedSizeBinary":
          return accessor(index, new fixed_size_binary_js_1.FixedSizeBinary());
        case "FixedSizeList":
          return accessor(index, new fixed_size_list_js_1.FixedSizeList());
        case "Map":
          return accessor(index, new map_js_1.Map());
        case "Duration":
          return accessor(index, new duration_js_1.Duration());
        case "LargeBinary":
          return accessor(index, new large_binary_js_1.LargeBinary());
        case "LargeUtf8":
          return accessor(index, new large_utf8_js_1.LargeUtf8());
        case "LargeList":
          return accessor(index, new large_list_js_1.LargeList());
        case "RunEndEncoded":
          return accessor(index, new run_end_encoded_js_1.RunEndEncoded());
        default:
          return null;
      }
    }
    exports2.unionListToType = unionListToType;
  }
});

// node_modules/apache-arrow/fb/field.js
var require_field = __commonJS({
  "node_modules/apache-arrow/fb/field.js"(exports2) {
    "use strict";
    Object.defineProperty(exports2, "__esModule", { value: true });
    exports2.Field = void 0;
    var flatbuffers = (init_flatbuffers(), __toCommonJS(flatbuffers_exports));
    var dictionary_encoding_js_1 = require_dictionary_encoding();
    var key_value_js_1 = require_key_value();
    var type_js_1 = require_type();
    var Field = class _Field {
      constructor() {
        this.bb = null;
        this.bb_pos = 0;
      }
      __init(i, bb) {
        this.bb_pos = i;
        this.bb = bb;
        return this;
      }
      static getRootAsField(bb, obj) {
        return (obj || new _Field()).__init(bb.readInt32(bb.position()) + bb.position(), bb);
      }
      static getSizePrefixedRootAsField(bb, obj) {
        bb.setPosition(bb.position() + flatbuffers.SIZE_PREFIX_LENGTH);
        return (obj || new _Field()).__init(bb.readInt32(bb.position()) + bb.position(), bb);
      }
      name(optionalEncoding) {
        const offset = this.bb.__offset(this.bb_pos, 4);
        return offset ? this.bb.__string(this.bb_pos + offset, optionalEncoding) : null;
      }
      /**
       * Whether or not this field can contain nulls. Should be true in general.
       */
      nullable() {
        const offset = this.bb.__offset(this.bb_pos, 6);
        return offset ? !!this.bb.readInt8(this.bb_pos + offset) : false;
      }
      typeType() {
        const offset = this.bb.__offset(this.bb_pos, 8);
        return offset ? this.bb.readUint8(this.bb_pos + offset) : type_js_1.Type.NONE;
      }
      /**
       * This is the type of the decoded value if the field is dictionary encoded.
       */
      type(obj) {
        const offset = this.bb.__offset(this.bb_pos, 10);
        return offset ? this.bb.__union(obj, this.bb_pos + offset) : null;
      }
      /**
       * Present only if the field is dictionary encoded.
       */
      dictionary(obj) {
        const offset = this.bb.__offset(this.bb_pos, 12);
        return offset ? (obj || new dictionary_encoding_js_1.DictionaryEncoding()).__init(this.bb.__indirect(this.bb_pos + offset), this.bb) : null;
      }
      /**
       * children apply only to nested data types like Struct, List and Union. For
       * primitive types children will have length 0.
       */
      children(index, obj) {
        const offset = this.bb.__offset(this.bb_pos, 14);
        return offset ? (obj || new _Field()).__init(this.bb.__indirect(this.bb.__vector(this.bb_pos + offset) + index * 4), this.bb) : null;
      }
      childrenLength() {
        const offset = this.bb.__offset(this.bb_pos, 14);
        return offset ? this.bb.__vector_len(this.bb_pos + offset) : 0;
      }
      /**
       * User-defined metadata
       */
      customMetadata(index, obj) {
        const offset = this.bb.__offset(this.bb_pos, 16);
        return offset ? (obj || new key_value_js_1.KeyValue()).__init(this.bb.__indirect(this.bb.__vector(this.bb_pos + offset) + index * 4), this.bb) : null;
      }
      customMetadataLength() {
        const offset = this.bb.__offset(this.bb_pos, 16);
        return offset ? this.bb.__vector_len(this.bb_pos + offset) : 0;
      }
      static startField(builder) {
        builder.startObject(7);
      }
      static addName(builder, nameOffset) {
        builder.addFieldOffset(0, nameOffset, 0);
      }
      static addNullable(builder, nullable) {
        builder.addFieldInt8(1, +nullable, 0);
      }
      static addTypeType(builder, typeType) {
        builder.addFieldInt8(2, typeType, type_js_1.Type.NONE);
      }
      static addType(builder, typeOffset) {
        builder.addFieldOffset(3, typeOffset, 0);
      }
      static addDictionary(builder, dictionaryOffset) {
        builder.addFieldOffset(4, dictionaryOffset, 0);
      }
      static addChildren(builder, childrenOffset) {
        builder.addFieldOffset(5, childrenOffset, 0);
      }
      static createChildrenVector(builder, data) {
        builder.startVector(4, data.length, 4);
        for (let i = data.length - 1; i >= 0; i--) {
          builder.addOffset(data[i]);
        }
        return builder.endVector();
      }
      static startChildrenVector(builder, numElems) {
        builder.startVector(4, numElems, 4);
      }
      static addCustomMetadata(builder, customMetadataOffset) {
        builder.addFieldOffset(6, customMetadataOffset, 0);
      }
      static createCustomMetadataVector(builder, data) {
        builder.startVector(4, data.length, 4);
        for (let i = data.length - 1; i >= 0; i--) {
          builder.addOffset(data[i]);
        }
        return builder.endVector();
      }
      static startCustomMetadataVector(builder, numElems) {
        builder.startVector(4, numElems, 4);
      }
      static endField(builder) {
        const offset = builder.endObject();
        return offset;
      }
    };
    exports2.Field = Field;
  }
});

// node_modules/apache-arrow/fb/schema.js
var require_schema = __commonJS({
  "node_modules/apache-arrow/fb/schema.js"(exports2) {
    "use strict";
    Object.defineProperty(exports2, "__esModule", { value: true });
    exports2.Schema = void 0;
    var flatbuffers = (init_flatbuffers(), __toCommonJS(flatbuffers_exports));
    var endianness_js_1 = require_endianness();
    var field_js_1 = require_field();
    var key_value_js_1 = require_key_value();
    var Schema = class _Schema {
      constructor() {
        this.bb = null;
        this.bb_pos = 0;
      }
      __init(i, bb) {
        this.bb_pos = i;
        this.bb = bb;
        return this;
      }
      static getRootAsSchema(bb, obj) {
        return (obj || new _Schema()).__init(bb.readInt32(bb.position()) + bb.position(), bb);
      }
      static getSizePrefixedRootAsSchema(bb, obj) {
        bb.setPosition(bb.position() + flatbuffers.SIZE_PREFIX_LENGTH);
        return (obj || new _Schema()).__init(bb.readInt32(bb.position()) + bb.position(), bb);
      }
      /**
       * endianness of the buffer
       * it is Little Endian by default
       * if endianness doesn't match the underlying system then the vectors need to be converted
       */
      endianness() {
        const offset = this.bb.__offset(this.bb_pos, 4);
        return offset ? this.bb.readInt16(this.bb_pos + offset) : endianness_js_1.Endianness.Little;
      }
      fields(index, obj) {
        const offset = this.bb.__offset(this.bb_pos, 6);
        return offset ? (obj || new field_js_1.Field()).__init(this.bb.__indirect(this.bb.__vector(this.bb_pos + offset) + index * 4), this.bb) : null;
      }
      fieldsLength() {
        const offset = this.bb.__offset(this.bb_pos, 6);
        return offset ? this.bb.__vector_len(this.bb_pos + offset) : 0;
      }
      customMetadata(index, obj) {
        const offset = this.bb.__offset(this.bb_pos, 8);
        return offset ? (obj || new key_value_js_1.KeyValue()).__init(this.bb.__indirect(this.bb.__vector(this.bb_pos + offset) + index * 4), this.bb) : null;
      }
      customMetadataLength() {
        const offset = this.bb.__offset(this.bb_pos, 8);
        return offset ? this.bb.__vector_len(this.bb_pos + offset) : 0;
      }
      /**
       * Features used in the stream/file.
       */
      features(index) {
        const offset = this.bb.__offset(this.bb_pos, 10);
        return offset ? this.bb.readInt64(this.bb.__vector(this.bb_pos + offset) + index * 8) : BigInt(0);
      }
      featuresLength() {
        const offset = this.bb.__offset(this.bb_pos, 10);
        return offset ? this.bb.__vector_len(this.bb_pos + offset) : 0;
      }
      static startSchema(builder) {
        builder.startObject(4);
      }
      static addEndianness(builder, endianness) {
        builder.addFieldInt16(0, endianness, endianness_js_1.Endianness.Little);
      }
      static addFields(builder, fieldsOffset) {
        builder.addFieldOffset(1, fieldsOffset, 0);
      }
      static createFieldsVector(builder, data) {
        builder.startVector(4, data.length, 4);
        for (let i = data.length - 1; i >= 0; i--) {
          builder.addOffset(data[i]);
        }
        return builder.endVector();
      }
      static startFieldsVector(builder, numElems) {
        builder.startVector(4, numElems, 4);
      }
      static addCustomMetadata(builder, customMetadataOffset) {
        builder.addFieldOffset(2, customMetadataOffset, 0);
      }
      static createCustomMetadataVector(builder, data) {
        builder.startVector(4, data.length, 4);
        for (let i = data.length - 1; i >= 0; i--) {
          builder.addOffset(data[i]);
        }
        return builder.endVector();
      }
      static startCustomMetadataVector(builder, numElems) {
        builder.startVector(4, numElems, 4);
      }
      static addFeatures(builder, featuresOffset) {
        builder.addFieldOffset(3, featuresOffset, 0);
      }
      static createFeaturesVector(builder, data) {
        builder.startVector(8, data.length, 8);
        for (let i = data.length - 1; i >= 0; i--) {
          builder.addInt64(data[i]);
        }
        return builder.endVector();
      }
      static startFeaturesVector(builder, numElems) {
        builder.startVector(8, numElems, 8);
      }
      static endSchema(builder) {
        const offset = builder.endObject();
        return offset;
      }
      static finishSchemaBuffer(builder, offset) {
        builder.finish(offset);
      }
      static finishSizePrefixedSchemaBuffer(builder, offset) {
        builder.finish(offset, void 0, true);
      }
      static createSchema(builder, endianness, fieldsOffset, customMetadataOffset, featuresOffset) {
        _Schema.startSchema(builder);
        _Schema.addEndianness(builder, endianness);
        _Schema.addFields(builder, fieldsOffset);
        _Schema.addCustomMetadata(builder, customMetadataOffset);
        _Schema.addFeatures(builder, featuresOffset);
        return _Schema.endSchema(builder);
      }
    };
    exports2.Schema = Schema;
  }
});

// node_modules/apache-arrow/fb/sparse-matrix-compressed-axis.js
var require_sparse_matrix_compressed_axis = __commonJS({
  "node_modules/apache-arrow/fb/sparse-matrix-compressed-axis.js"(exports2) {
    "use strict";
    Object.defineProperty(exports2, "__esModule", { value: true });
    exports2.SparseMatrixCompressedAxis = void 0;
    var SparseMatrixCompressedAxis;
    (function(SparseMatrixCompressedAxis2) {
      SparseMatrixCompressedAxis2[SparseMatrixCompressedAxis2["Row"] = 0] = "Row";
      SparseMatrixCompressedAxis2[SparseMatrixCompressedAxis2["Column"] = 1] = "Column";
    })(SparseMatrixCompressedAxis || (exports2.SparseMatrixCompressedAxis = SparseMatrixCompressedAxis = {}));
  }
});

// node_modules/apache-arrow/fb/sparse-matrix-index-csx.js
var require_sparse_matrix_index_csx = __commonJS({
  "node_modules/apache-arrow/fb/sparse-matrix-index-csx.js"(exports2) {
    "use strict";
    Object.defineProperty(exports2, "__esModule", { value: true });
    exports2.SparseMatrixIndexCSX = void 0;
    var flatbuffers = (init_flatbuffers(), __toCommonJS(flatbuffers_exports));
    var buffer_js_1 = require_buffer2();
    var int_js_1 = require_int();
    var sparse_matrix_compressed_axis_js_1 = require_sparse_matrix_compressed_axis();
    var SparseMatrixIndexCSX = class _SparseMatrixIndexCSX {
      constructor() {
        this.bb = null;
        this.bb_pos = 0;
      }
      __init(i, bb) {
        this.bb_pos = i;
        this.bb = bb;
        return this;
      }
      static getRootAsSparseMatrixIndexCSX(bb, obj) {
        return (obj || new _SparseMatrixIndexCSX()).__init(bb.readInt32(bb.position()) + bb.position(), bb);
      }
      static getSizePrefixedRootAsSparseMatrixIndexCSX(bb, obj) {
        bb.setPosition(bb.position() + flatbuffers.SIZE_PREFIX_LENGTH);
        return (obj || new _SparseMatrixIndexCSX()).__init(bb.readInt32(bb.position()) + bb.position(), bb);
      }
      /**
       * Which axis, row or column, is compressed
       */
      compressedAxis() {
        const offset = this.bb.__offset(this.bb_pos, 4);
        return offset ? this.bb.readInt16(this.bb_pos + offset) : sparse_matrix_compressed_axis_js_1.SparseMatrixCompressedAxis.Row;
      }
      /**
       * The type of values in indptrBuffer
       */
      indptrType(obj) {
        const offset = this.bb.__offset(this.bb_pos, 6);
        return offset ? (obj || new int_js_1.Int()).__init(this.bb.__indirect(this.bb_pos + offset), this.bb) : null;
      }
      /**
       * indptrBuffer stores the location and size of indptr array that
       * represents the range of the rows.
       * The i-th row spans from `indptr[i]` to `indptr[i+1]` in the data.
       * The length of this array is 1 + (the number of rows), and the type
       * of index value is long.
       *
       * For example, let X be the following 6x4 matrix:
       * ```text
       *   X := [[0, 1, 2, 0],
       *         [0, 0, 3, 0],
       *         [0, 4, 0, 5],
       *         [0, 0, 0, 0],
       *         [6, 0, 7, 8],
       *         [0, 9, 0, 0]].
       * ```
       * The array of non-zero values in X is:
       * ```text
       *   values(X) = [1, 2, 3, 4, 5, 6, 7, 8, 9].
       * ```
       * And the indptr of X is:
       * ```text
       *   indptr(X) = [0, 2, 3, 5, 5, 8, 10].
       * ```
       */
      indptrBuffer(obj) {
        const offset = this.bb.__offset(this.bb_pos, 8);
        return offset ? (obj || new buffer_js_1.Buffer()).__init(this.bb_pos + offset, this.bb) : null;
      }
      /**
       * The type of values in indicesBuffer
       */
      indicesType(obj) {
        const offset = this.bb.__offset(this.bb_pos, 10);
        return offset ? (obj || new int_js_1.Int()).__init(this.bb.__indirect(this.bb_pos + offset), this.bb) : null;
      }
      /**
       * indicesBuffer stores the location and size of the array that
       * contains the column indices of the corresponding non-zero values.
       * The type of index value is long.
       *
       * For example, the indices of the above X is:
       * ```text
       *   indices(X) = [1, 2, 2, 1, 3, 0, 2, 3, 1].
       * ```
       * Note that the indices are sorted in lexicographical order for each row.
       */
      indicesBuffer(obj) {
        const offset = this.bb.__offset(this.bb_pos, 12);
        return offset ? (obj || new buffer_js_1.Buffer()).__init(this.bb_pos + offset, this.bb) : null;
      }
      static startSparseMatrixIndexCSX(builder) {
        builder.startObject(5);
      }
      static addCompressedAxis(builder, compressedAxis) {
        builder.addFieldInt16(0, compressedAxis, sparse_matrix_compressed_axis_js_1.SparseMatrixCompressedAxis.Row);
      }
      static addIndptrType(builder, indptrTypeOffset) {
        builder.addFieldOffset(1, indptrTypeOffset, 0);
      }
      static addIndptrBuffer(builder, indptrBufferOffset) {
        builder.addFieldStruct(2, indptrBufferOffset, 0);
      }
      static addIndicesType(builder, indicesTypeOffset) {
        builder.addFieldOffset(3, indicesTypeOffset, 0);
      }
      static addIndicesBuffer(builder, indicesBufferOffset) {
        builder.addFieldStruct(4, indicesBufferOffset, 0);
      }
      static endSparseMatrixIndexCSX(builder) {
        const offset = builder.endObject();
        builder.requiredField(offset, 6);
        builder.requiredField(offset, 8);
        builder.requiredField(offset, 10);
        builder.requiredField(offset, 12);
        return offset;
      }
    };
    exports2.SparseMatrixIndexCSX = SparseMatrixIndexCSX;
  }
});

// node_modules/apache-arrow/fb/sparse-tensor-index-coo.js
var require_sparse_tensor_index_coo = __commonJS({
  "node_modules/apache-arrow/fb/sparse-tensor-index-coo.js"(exports2) {
    "use strict";
    Object.defineProperty(exports2, "__esModule", { value: true });
    exports2.SparseTensorIndexCOO = void 0;
    var flatbuffers = (init_flatbuffers(), __toCommonJS(flatbuffers_exports));
    var buffer_js_1 = require_buffer2();
    var int_js_1 = require_int();
    var SparseTensorIndexCOO = class _SparseTensorIndexCOO {
      constructor() {
        this.bb = null;
        this.bb_pos = 0;
      }
      __init(i, bb) {
        this.bb_pos = i;
        this.bb = bb;
        return this;
      }
      static getRootAsSparseTensorIndexCOO(bb, obj) {
        return (obj || new _SparseTensorIndexCOO()).__init(bb.readInt32(bb.position()) + bb.position(), bb);
      }
      static getSizePrefixedRootAsSparseTensorIndexCOO(bb, obj) {
        bb.setPosition(bb.position() + flatbuffers.SIZE_PREFIX_LENGTH);
        return (obj || new _SparseTensorIndexCOO()).__init(bb.readInt32(bb.position()) + bb.position(), bb);
      }
      /**
       * The type of values in indicesBuffer
       */
      indicesType(obj) {
        const offset = this.bb.__offset(this.bb_pos, 4);
        return offset ? (obj || new int_js_1.Int()).__init(this.bb.__indirect(this.bb_pos + offset), this.bb) : null;
      }
      /**
       * Non-negative byte offsets to advance one value cell along each dimension
       * If omitted, default to row-major order (C-like).
       */
      indicesStrides(index) {
        const offset = this.bb.__offset(this.bb_pos, 6);
        return offset ? this.bb.readInt64(this.bb.__vector(this.bb_pos + offset) + index * 8) : BigInt(0);
      }
      indicesStridesLength() {
        const offset = this.bb.__offset(this.bb_pos, 6);
        return offset ? this.bb.__vector_len(this.bb_pos + offset) : 0;
      }
      /**
       * The location and size of the indices matrix's data
       */
      indicesBuffer(obj) {
        const offset = this.bb.__offset(this.bb_pos, 8);
        return offset ? (obj || new buffer_js_1.Buffer()).__init(this.bb_pos + offset, this.bb) : null;
      }
      /**
       * This flag is true if and only if the indices matrix is sorted in
       * row-major order, and does not have duplicated entries.
       * This sort order is the same as of Tensorflow's SparseTensor,
       * but it is inverse order of SciPy's canonical coo_matrix
       * (SciPy employs column-major order for its coo_matrix).
       */
      isCanonical() {
        const offset = this.bb.__offset(this.bb_pos, 10);
        return offset ? !!this.bb.readInt8(this.bb_pos + offset) : false;
      }
      static startSparseTensorIndexCOO(builder) {
        builder.startObject(4);
      }
      static addIndicesType(builder, indicesTypeOffset) {
        builder.addFieldOffset(0, indicesTypeOffset, 0);
      }
      static addIndicesStrides(builder, indicesStridesOffset) {
        builder.addFieldOffset(1, indicesStridesOffset, 0);
      }
      static createIndicesStridesVector(builder, data) {
        builder.startVector(8, data.length, 8);
        for (let i = data.length - 1; i >= 0; i--) {
          builder.addInt64(data[i]);
        }
        return builder.endVector();
      }
      static startIndicesStridesVector(builder, numElems) {
        builder.startVector(8, numElems, 8);
      }
      static addIndicesBuffer(builder, indicesBufferOffset) {
        builder.addFieldStruct(2, indicesBufferOffset, 0);
      }
      static addIsCanonical(builder, isCanonical) {
        builder.addFieldInt8(3, +isCanonical, 0);
      }
      static endSparseTensorIndexCOO(builder) {
        const offset = builder.endObject();
        builder.requiredField(offset, 4);
        builder.requiredField(offset, 8);
        return offset;
      }
    };
    exports2.SparseTensorIndexCOO = SparseTensorIndexCOO;
  }
});

// node_modules/apache-arrow/fb/sparse-tensor-index-csf.js
var require_sparse_tensor_index_csf = __commonJS({
  "node_modules/apache-arrow/fb/sparse-tensor-index-csf.js"(exports2) {
    "use strict";
    Object.defineProperty(exports2, "__esModule", { value: true });
    exports2.SparseTensorIndexCSF = void 0;
    var flatbuffers = (init_flatbuffers(), __toCommonJS(flatbuffers_exports));
    var buffer_js_1 = require_buffer2();
    var int_js_1 = require_int();
    var SparseTensorIndexCSF = class _SparseTensorIndexCSF {
      constructor() {
        this.bb = null;
        this.bb_pos = 0;
      }
      __init(i, bb) {
        this.bb_pos = i;
        this.bb = bb;
        return this;
      }
      static getRootAsSparseTensorIndexCSF(bb, obj) {
        return (obj || new _SparseTensorIndexCSF()).__init(bb.readInt32(bb.position()) + bb.position(), bb);
      }
      static getSizePrefixedRootAsSparseTensorIndexCSF(bb, obj) {
        bb.setPosition(bb.position() + flatbuffers.SIZE_PREFIX_LENGTH);
        return (obj || new _SparseTensorIndexCSF()).__init(bb.readInt32(bb.position()) + bb.position(), bb);
      }
      /**
       * CSF is a generalization of compressed sparse row (CSR) index.
       * See [smith2017knl](http://shaden.io/pub-files/smith2017knl.pdf)
       *
       * CSF index recursively compresses each dimension of a tensor into a set
       * of prefix trees. Each path from a root to leaf forms one tensor
       * non-zero index. CSF is implemented with two arrays of buffers and one
       * arrays of integers.
       *
       * For example, let X be a 2x3x4x5 tensor and let it have the following
       * 8 non-zero values:
       * ```text
       *   X[0, 0, 0, 1] := 1
       *   X[0, 0, 0, 2] := 2
       *   X[0, 1, 0, 0] := 3
       *   X[0, 1, 0, 2] := 4
       *   X[0, 1, 1, 0] := 5
       *   X[1, 1, 1, 0] := 6
       *   X[1, 1, 1, 1] := 7
       *   X[1, 1, 1, 2] := 8
       * ```
       * As a prefix tree this would be represented as:
       * ```text
       *         0          1
       *        / \         |
       *       0   1        1
       *      /   / \       |
       *     0   0   1      1
       *    /|  /|   |    /| |
       *   1 2 0 2   0   0 1 2
       * ```
       * The type of values in indptrBuffers
       */
      indptrType(obj) {
        const offset = this.bb.__offset(this.bb_pos, 4);
        return offset ? (obj || new int_js_1.Int()).__init(this.bb.__indirect(this.bb_pos + offset), this.bb) : null;
      }
      /**
       * indptrBuffers stores the sparsity structure.
       * Each two consecutive dimensions in a tensor correspond to a buffer in
       * indptrBuffers. A pair of consecutive values at `indptrBuffers[dim][i]`
       * and `indptrBuffers[dim][i + 1]` signify a range of nodes in
       * `indicesBuffers[dim + 1]` who are children of `indicesBuffers[dim][i]` node.
       *
       * For example, the indptrBuffers for the above X is:
       * ```text
       *   indptrBuffer(X) = [
       *                       [0, 2, 3],
       *                       [0, 1, 3, 4],
       *                       [0, 2, 4, 5, 8]
       *                     ].
       * ```
       */
      indptrBuffers(index, obj) {
        const offset = this.bb.__offset(this.bb_pos, 6);
        return offset ? (obj || new buffer_js_1.Buffer()).__init(this.bb.__vector(this.bb_pos + offset) + index * 16, this.bb) : null;
      }
      indptrBuffersLength() {
        const offset = this.bb.__offset(this.bb_pos, 6);
        return offset ? this.bb.__vector_len(this.bb_pos + offset) : 0;
      }
      /**
       * The type of values in indicesBuffers
       */
      indicesType(obj) {
        const offset = this.bb.__offset(this.bb_pos, 8);
        return offset ? (obj || new int_js_1.Int()).__init(this.bb.__indirect(this.bb_pos + offset), this.bb) : null;
      }
      /**
       * indicesBuffers stores values of nodes.
       * Each tensor dimension corresponds to a buffer in indicesBuffers.
       * For example, the indicesBuffers for the above X is:
       * ```text
       *   indicesBuffer(X) = [
       *                        [0, 1],
       *                        [0, 1, 1],
       *                        [0, 0, 1, 1],
       *                        [1, 2, 0, 2, 0, 0, 1, 2]
       *                      ].
       * ```
       */
      indicesBuffers(index, obj) {
        const offset = this.bb.__offset(this.bb_pos, 10);
        return offset ? (obj || new buffer_js_1.Buffer()).__init(this.bb.__vector(this.bb_pos + offset) + index * 16, this.bb) : null;
      }
      indicesBuffersLength() {
        const offset = this.bb.__offset(this.bb_pos, 10);
        return offset ? this.bb.__vector_len(this.bb_pos + offset) : 0;
      }
      /**
       * axisOrder stores the sequence in which dimensions were traversed to
       * produce the prefix tree.
       * For example, the axisOrder for the above X is:
       * ```text
       *   axisOrder(X) = [0, 1, 2, 3].
       * ```
       */
      axisOrder(index) {
        const offset = this.bb.__offset(this.bb_pos, 12);
        return offset ? this.bb.readInt32(this.bb.__vector(this.bb_pos + offset) + index * 4) : 0;
      }
      axisOrderLength() {
        const offset = this.bb.__offset(this.bb_pos, 12);
        return offset ? this.bb.__vector_len(this.bb_pos + offset) : 0;
      }
      axisOrderArray() {
        const offset = this.bb.__offset(this.bb_pos, 12);
        return offset ? new Int32Array(this.bb.bytes().buffer, this.bb.bytes().byteOffset + this.bb.__vector(this.bb_pos + offset), this.bb.__vector_len(this.bb_pos + offset)) : null;
      }
      static startSparseTensorIndexCSF(builder) {
        builder.startObject(5);
      }
      static addIndptrType(builder, indptrTypeOffset) {
        builder.addFieldOffset(0, indptrTypeOffset, 0);
      }
      static addIndptrBuffers(builder, indptrBuffersOffset) {
        builder.addFieldOffset(1, indptrBuffersOffset, 0);
      }
      static startIndptrBuffersVector(builder, numElems) {
        builder.startVector(16, numElems, 8);
      }
      static addIndicesType(builder, indicesTypeOffset) {
        builder.addFieldOffset(2, indicesTypeOffset, 0);
      }
      static addIndicesBuffers(builder, indicesBuffersOffset) {
        builder.addFieldOffset(3, indicesBuffersOffset, 0);
      }
      static startIndicesBuffersVector(builder, numElems) {
        builder.startVector(16, numElems, 8);
      }
      static addAxisOrder(builder, axisOrderOffset) {
        builder.addFieldOffset(4, axisOrderOffset, 0);
      }
      static createAxisOrderVector(builder, data) {
        builder.startVector(4, data.length, 4);
        for (let i = data.length - 1; i >= 0; i--) {
          builder.addInt32(data[i]);
        }
        return builder.endVector();
      }
      static startAxisOrderVector(builder, numElems) {
        builder.startVector(4, numElems, 4);
      }
      static endSparseTensorIndexCSF(builder) {
        const offset = builder.endObject();
        builder.requiredField(offset, 4);
        builder.requiredField(offset, 6);
        builder.requiredField(offset, 8);
        builder.requiredField(offset, 10);
        builder.requiredField(offset, 12);
        return offset;
      }
    };
    exports2.SparseTensorIndexCSF = SparseTensorIndexCSF;
  }
});

// node_modules/apache-arrow/fb/sparse-tensor-index.js
var require_sparse_tensor_index = __commonJS({
  "node_modules/apache-arrow/fb/sparse-tensor-index.js"(exports2) {
    "use strict";
    Object.defineProperty(exports2, "__esModule", { value: true });
    exports2.unionListToSparseTensorIndex = exports2.unionToSparseTensorIndex = exports2.SparseTensorIndex = void 0;
    var sparse_matrix_index_csx_js_1 = require_sparse_matrix_index_csx();
    var sparse_tensor_index_coo_js_1 = require_sparse_tensor_index_coo();
    var sparse_tensor_index_csf_js_1 = require_sparse_tensor_index_csf();
    var SparseTensorIndex;
    (function(SparseTensorIndex2) {
      SparseTensorIndex2[SparseTensorIndex2["NONE"] = 0] = "NONE";
      SparseTensorIndex2[SparseTensorIndex2["SparseTensorIndexCOO"] = 1] = "SparseTensorIndexCOO";
      SparseTensorIndex2[SparseTensorIndex2["SparseMatrixIndexCSX"] = 2] = "SparseMatrixIndexCSX";
      SparseTensorIndex2[SparseTensorIndex2["SparseTensorIndexCSF"] = 3] = "SparseTensorIndexCSF";
    })(SparseTensorIndex || (exports2.SparseTensorIndex = SparseTensorIndex = {}));
    function unionToSparseTensorIndex(type, accessor) {
      switch (SparseTensorIndex[type]) {
        case "NONE":
          return null;
        case "SparseTensorIndexCOO":
          return accessor(new sparse_tensor_index_coo_js_1.SparseTensorIndexCOO());
        case "SparseMatrixIndexCSX":
          return accessor(new sparse_matrix_index_csx_js_1.SparseMatrixIndexCSX());
        case "SparseTensorIndexCSF":
          return accessor(new sparse_tensor_index_csf_js_1.SparseTensorIndexCSF());
        default:
          return null;
      }
    }
    exports2.unionToSparseTensorIndex = unionToSparseTensorIndex;
    function unionListToSparseTensorIndex(type, accessor, index) {
      switch (SparseTensorIndex[type]) {
        case "NONE":
          return null;
        case "SparseTensorIndexCOO":
          return accessor(index, new sparse_tensor_index_coo_js_1.SparseTensorIndexCOO());
        case "SparseMatrixIndexCSX":
          return accessor(index, new sparse_matrix_index_csx_js_1.SparseMatrixIndexCSX());
        case "SparseTensorIndexCSF":
          return accessor(index, new sparse_tensor_index_csf_js_1.SparseTensorIndexCSF());
        default:
          return null;
      }
    }
    exports2.unionListToSparseTensorIndex = unionListToSparseTensorIndex;
  }
});

// node_modules/apache-arrow/fb/tensor-dim.js
var require_tensor_dim = __commonJS({
  "node_modules/apache-arrow/fb/tensor-dim.js"(exports2) {
    "use strict";
    Object.defineProperty(exports2, "__esModule", { value: true });
    exports2.TensorDim = void 0;
    var flatbuffers = (init_flatbuffers(), __toCommonJS(flatbuffers_exports));
    var TensorDim = class _TensorDim {
      constructor() {
        this.bb = null;
        this.bb_pos = 0;
      }
      __init(i, bb) {
        this.bb_pos = i;
        this.bb = bb;
        return this;
      }
      static getRootAsTensorDim(bb, obj) {
        return (obj || new _TensorDim()).__init(bb.readInt32(bb.position()) + bb.position(), bb);
      }
      static getSizePrefixedRootAsTensorDim(bb, obj) {
        bb.setPosition(bb.position() + flatbuffers.SIZE_PREFIX_LENGTH);
        return (obj || new _TensorDim()).__init(bb.readInt32(bb.position()) + bb.position(), bb);
      }
      /**
       * Length of dimension
       */
      size() {
        const offset = this.bb.__offset(this.bb_pos, 4);
        return offset ? this.bb.readInt64(this.bb_pos + offset) : BigInt("0");
      }
      name(optionalEncoding) {
        const offset = this.bb.__offset(this.bb_pos, 6);
        return offset ? this.bb.__string(this.bb_pos + offset, optionalEncoding) : null;
      }
      static startTensorDim(builder) {
        builder.startObject(2);
      }
      static addSize(builder, size) {
        builder.addFieldInt64(0, size, BigInt("0"));
      }
      static addName(builder, nameOffset) {
        builder.addFieldOffset(1, nameOffset, 0);
      }
      static endTensorDim(builder) {
        const offset = builder.endObject();
        return offset;
      }
      static createTensorDim(builder, size, nameOffset) {
        _TensorDim.startTensorDim(builder);
        _TensorDim.addSize(builder, size);
        _TensorDim.addName(builder, nameOffset);
        return _TensorDim.endTensorDim(builder);
      }
    };
    exports2.TensorDim = TensorDim;
  }
});

// node_modules/apache-arrow/fb/sparse-tensor.js
var require_sparse_tensor = __commonJS({
  "node_modules/apache-arrow/fb/sparse-tensor.js"(exports2) {
    "use strict";
    Object.defineProperty(exports2, "__esModule", { value: true });
    exports2.SparseTensor = void 0;
    var flatbuffers = (init_flatbuffers(), __toCommonJS(flatbuffers_exports));
    var buffer_js_1 = require_buffer2();
    var sparse_tensor_index_js_1 = require_sparse_tensor_index();
    var tensor_dim_js_1 = require_tensor_dim();
    var type_js_1 = require_type();
    var SparseTensor = class _SparseTensor {
      constructor() {
        this.bb = null;
        this.bb_pos = 0;
      }
      __init(i, bb) {
        this.bb_pos = i;
        this.bb = bb;
        return this;
      }
      static getRootAsSparseTensor(bb, obj) {
        return (obj || new _SparseTensor()).__init(bb.readInt32(bb.position()) + bb.position(), bb);
      }
      static getSizePrefixedRootAsSparseTensor(bb, obj) {
        bb.setPosition(bb.position() + flatbuffers.SIZE_PREFIX_LENGTH);
        return (obj || new _SparseTensor()).__init(bb.readInt32(bb.position()) + bb.position(), bb);
      }
      typeType() {
        const offset = this.bb.__offset(this.bb_pos, 4);
        return offset ? this.bb.readUint8(this.bb_pos + offset) : type_js_1.Type.NONE;
      }
      /**
       * The type of data contained in a value cell.
       * Currently only fixed-width value types are supported,
       * no strings or nested types.
       */
      type(obj) {
        const offset = this.bb.__offset(this.bb_pos, 6);
        return offset ? this.bb.__union(obj, this.bb_pos + offset) : null;
      }
      /**
       * The dimensions of the tensor, optionally named.
       */
      shape(index, obj) {
        const offset = this.bb.__offset(this.bb_pos, 8);
        return offset ? (obj || new tensor_dim_js_1.TensorDim()).__init(this.bb.__indirect(this.bb.__vector(this.bb_pos + offset) + index * 4), this.bb) : null;
      }
      shapeLength() {
        const offset = this.bb.__offset(this.bb_pos, 8);
        return offset ? this.bb.__vector_len(this.bb_pos + offset) : 0;
      }
      /**
       * The number of non-zero values in a sparse tensor.
       */
      nonZeroLength() {
        const offset = this.bb.__offset(this.bb_pos, 10);
        return offset ? this.bb.readInt64(this.bb_pos + offset) : BigInt("0");
      }
      sparseIndexType() {
        const offset = this.bb.__offset(this.bb_pos, 12);
        return offset ? this.bb.readUint8(this.bb_pos + offset) : sparse_tensor_index_js_1.SparseTensorIndex.NONE;
      }
      /**
       * Sparse tensor index
       */
      sparseIndex(obj) {
        const offset = this.bb.__offset(this.bb_pos, 14);
        return offset ? this.bb.__union(obj, this.bb_pos + offset) : null;
      }
      /**
       * The location and size of the tensor's data
       */
      data(obj) {
        const offset = this.bb.__offset(this.bb_pos, 16);
        return offset ? (obj || new buffer_js_1.Buffer()).__init(this.bb_pos + offset, this.bb) : null;
      }
      static startSparseTensor(builder) {
        builder.startObject(7);
      }
      static addTypeType(builder, typeType) {
        builder.addFieldInt8(0, typeType, type_js_1.Type.NONE);
      }
      static addType(builder, typeOffset) {
        builder.addFieldOffset(1, typeOffset, 0);
      }
      static addShape(builder, shapeOffset) {
        builder.addFieldOffset(2, shapeOffset, 0);
      }
      static createShapeVector(builder, data) {
        builder.startVector(4, data.length, 4);
        for (let i = data.length - 1; i >= 0; i--) {
          builder.addOffset(data[i]);
        }
        return builder.endVector();
      }
      static startShapeVector(builder, numElems) {
        builder.startVector(4, numElems, 4);
      }
      static addNonZeroLength(builder, nonZeroLength) {
        builder.addFieldInt64(3, nonZeroLength, BigInt("0"));
      }
      static addSparseIndexType(builder, sparseIndexType) {
        builder.addFieldInt8(4, sparseIndexType, sparse_tensor_index_js_1.SparseTensorIndex.NONE);
      }
      static addSparseIndex(builder, sparseIndexOffset) {
        builder.addFieldOffset(5, sparseIndexOffset, 0);
      }
      static addData(builder, dataOffset) {
        builder.addFieldStruct(6, dataOffset, 0);
      }
      static endSparseTensor(builder) {
        const offset = builder.endObject();
        builder.requiredField(offset, 6);
        builder.requiredField(offset, 8);
        builder.requiredField(offset, 14);
        builder.requiredField(offset, 16);
        return offset;
      }
      static finishSparseTensorBuffer(builder, offset) {
        builder.finish(offset);
      }
      static finishSizePrefixedSparseTensorBuffer(builder, offset) {
        builder.finish(offset, void 0, true);
      }
    };
    exports2.SparseTensor = SparseTensor;
  }
});

// node_modules/apache-arrow/fb/tensor.js
var require_tensor = __commonJS({
  "node_modules/apache-arrow/fb/tensor.js"(exports2) {
    "use strict";
    Object.defineProperty(exports2, "__esModule", { value: true });
    exports2.Tensor = void 0;
    var flatbuffers = (init_flatbuffers(), __toCommonJS(flatbuffers_exports));
    var buffer_js_1 = require_buffer2();
    var tensor_dim_js_1 = require_tensor_dim();
    var type_js_1 = require_type();
    var Tensor = class _Tensor {
      constructor() {
        this.bb = null;
        this.bb_pos = 0;
      }
      __init(i, bb) {
        this.bb_pos = i;
        this.bb = bb;
        return this;
      }
      static getRootAsTensor(bb, obj) {
        return (obj || new _Tensor()).__init(bb.readInt32(bb.position()) + bb.position(), bb);
      }
      static getSizePrefixedRootAsTensor(bb, obj) {
        bb.setPosition(bb.position() + flatbuffers.SIZE_PREFIX_LENGTH);
        return (obj || new _Tensor()).__init(bb.readInt32(bb.position()) + bb.position(), bb);
      }
      typeType() {
        const offset = this.bb.__offset(this.bb_pos, 4);
        return offset ? this.bb.readUint8(this.bb_pos + offset) : type_js_1.Type.NONE;
      }
      /**
       * The type of data contained in a value cell. Currently only fixed-width
       * value types are supported, no strings or nested types
       */
      type(obj) {
        const offset = this.bb.__offset(this.bb_pos, 6);
        return offset ? this.bb.__union(obj, this.bb_pos + offset) : null;
      }
      /**
       * The dimensions of the tensor, optionally named
       */
      shape(index, obj) {
        const offset = this.bb.__offset(this.bb_pos, 8);
        return offset ? (obj || new tensor_dim_js_1.TensorDim()).__init(this.bb.__indirect(this.bb.__vector(this.bb_pos + offset) + index * 4), this.bb) : null;
      }
      shapeLength() {
        const offset = this.bb.__offset(this.bb_pos, 8);
        return offset ? this.bb.__vector_len(this.bb_pos + offset) : 0;
      }
      /**
       * Non-negative byte offsets to advance one value cell along each dimension
       * If omitted, default to row-major order (C-like).
       */
      strides(index) {
        const offset = this.bb.__offset(this.bb_pos, 10);
        return offset ? this.bb.readInt64(this.bb.__vector(this.bb_pos + offset) + index * 8) : BigInt(0);
      }
      stridesLength() {
        const offset = this.bb.__offset(this.bb_pos, 10);
        return offset ? this.bb.__vector_len(this.bb_pos + offset) : 0;
      }
      /**
       * The location and size of the tensor's data
       */
      data(obj) {
        const offset = this.bb.__offset(this.bb_pos, 12);
        return offset ? (obj || new buffer_js_1.Buffer()).__init(this.bb_pos + offset, this.bb) : null;
      }
      static startTensor(builder) {
        builder.startObject(5);
      }
      static addTypeType(builder, typeType) {
        builder.addFieldInt8(0, typeType, type_js_1.Type.NONE);
      }
      static addType(builder, typeOffset) {
        builder.addFieldOffset(1, typeOffset, 0);
      }
      static addShape(builder, shapeOffset) {
        builder.addFieldOffset(2, shapeOffset, 0);
      }
      static createShapeVector(builder, data) {
        builder.startVector(4, data.length, 4);
        for (let i = data.length - 1; i >= 0; i--) {
          builder.addOffset(data[i]);
        }
        return builder.endVector();
      }
      static startShapeVector(builder, numElems) {
        builder.startVector(4, numElems, 4);
      }
      static addStrides(builder, stridesOffset) {
        builder.addFieldOffset(3, stridesOffset, 0);
      }
      static createStridesVector(builder, data) {
        builder.startVector(8, data.length, 8);
        for (let i = data.length - 1; i >= 0; i--) {
          builder.addInt64(data[i]);
        }
        return builder.endVector();
      }
      static startStridesVector(builder, numElems) {
        builder.startVector(8, numElems, 8);
      }
      static addData(builder, dataOffset) {
        builder.addFieldStruct(4, dataOffset, 0);
      }
      static endTensor(builder) {
        const offset = builder.endObject();
        builder.requiredField(offset, 6);
        builder.requiredField(offset, 8);
        builder.requiredField(offset, 12);
        return offset;
      }
      static finishTensorBuffer(builder, offset) {
        builder.finish(offset);
      }
      static finishSizePrefixedTensorBuffer(builder, offset) {
        builder.finish(offset, void 0, true);
      }
    };
    exports2.Tensor = Tensor;
  }
});

// node_modules/apache-arrow/fb/message-header.js
var require_message_header = __commonJS({
  "node_modules/apache-arrow/fb/message-header.js"(exports2) {
    "use strict";
    Object.defineProperty(exports2, "__esModule", { value: true });
    exports2.unionListToMessageHeader = exports2.unionToMessageHeader = exports2.MessageHeader = void 0;
    var dictionary_batch_js_1 = require_dictionary_batch();
    var record_batch_js_1 = require_record_batch();
    var schema_js_1 = require_schema();
    var sparse_tensor_js_1 = require_sparse_tensor();
    var tensor_js_1 = require_tensor();
    var MessageHeader;
    (function(MessageHeader2) {
      MessageHeader2[MessageHeader2["NONE"] = 0] = "NONE";
      MessageHeader2[MessageHeader2["Schema"] = 1] = "Schema";
      MessageHeader2[MessageHeader2["DictionaryBatch"] = 2] = "DictionaryBatch";
      MessageHeader2[MessageHeader2["RecordBatch"] = 3] = "RecordBatch";
      MessageHeader2[MessageHeader2["Tensor"] = 4] = "Tensor";
      MessageHeader2[MessageHeader2["SparseTensor"] = 5] = "SparseTensor";
    })(MessageHeader || (exports2.MessageHeader = MessageHeader = {}));
    function unionToMessageHeader(type, accessor) {
      switch (MessageHeader[type]) {
        case "NONE":
          return null;
        case "Schema":
          return accessor(new schema_js_1.Schema());
        case "DictionaryBatch":
          return accessor(new dictionary_batch_js_1.DictionaryBatch());
        case "RecordBatch":
          return accessor(new record_batch_js_1.RecordBatch());
        case "Tensor":
          return accessor(new tensor_js_1.Tensor());
        case "SparseTensor":
          return accessor(new sparse_tensor_js_1.SparseTensor());
        default:
          return null;
      }
    }
    exports2.unionToMessageHeader = unionToMessageHeader;
    function unionListToMessageHeader(type, accessor, index) {
      switch (MessageHeader[type]) {
        case "NONE":
          return null;
        case "Schema":
          return accessor(index, new schema_js_1.Schema());
        case "DictionaryBatch":
          return accessor(index, new dictionary_batch_js_1.DictionaryBatch());
        case "RecordBatch":
          return accessor(index, new record_batch_js_1.RecordBatch());
        case "Tensor":
          return accessor(index, new tensor_js_1.Tensor());
        case "SparseTensor":
          return accessor(index, new sparse_tensor_js_1.SparseTensor());
        default:
          return null;
      }
    }
    exports2.unionListToMessageHeader = unionListToMessageHeader;
  }
});

// node_modules/apache-arrow/enum.js
var require_enum = __commonJS({
  "node_modules/apache-arrow/enum.js"(exports2) {
    "use strict";
    Object.defineProperty(exports2, "__esModule", { value: true });
    exports2.BufferType = exports2.Type = exports2.MessageHeader = exports2.IntervalUnit = exports2.TimeUnit = exports2.DateUnit = exports2.Precision = exports2.UnionMode = exports2.MetadataVersion = void 0;
    var metadata_version_js_1 = require_metadata_version();
    Object.defineProperty(exports2, "MetadataVersion", { enumerable: true, get: function() {
      return metadata_version_js_1.MetadataVersion;
    } });
    var union_mode_js_1 = require_union_mode();
    Object.defineProperty(exports2, "UnionMode", { enumerable: true, get: function() {
      return union_mode_js_1.UnionMode;
    } });
    var precision_js_1 = require_precision();
    Object.defineProperty(exports2, "Precision", { enumerable: true, get: function() {
      return precision_js_1.Precision;
    } });
    var date_unit_js_1 = require_date_unit();
    Object.defineProperty(exports2, "DateUnit", { enumerable: true, get: function() {
      return date_unit_js_1.DateUnit;
    } });
    var time_unit_js_1 = require_time_unit();
    Object.defineProperty(exports2, "TimeUnit", { enumerable: true, get: function() {
      return time_unit_js_1.TimeUnit;
    } });
    var interval_unit_js_1 = require_interval_unit();
    Object.defineProperty(exports2, "IntervalUnit", { enumerable: true, get: function() {
      return interval_unit_js_1.IntervalUnit;
    } });
    var message_header_js_1 = require_message_header();
    Object.defineProperty(exports2, "MessageHeader", { enumerable: true, get: function() {
      return message_header_js_1.MessageHeader;
    } });
    var Type;
    (function(Type2) {
      Type2[Type2["NONE"] = 0] = "NONE";
      Type2[Type2["Null"] = 1] = "Null";
      Type2[Type2["Int"] = 2] = "Int";
      Type2[Type2["Float"] = 3] = "Float";
      Type2[Type2["Binary"] = 4] = "Binary";
      Type2[Type2["Utf8"] = 5] = "Utf8";
      Type2[Type2["Bool"] = 6] = "Bool";
      Type2[Type2["Decimal"] = 7] = "Decimal";
      Type2[Type2["Date"] = 8] = "Date";
      Type2[Type2["Time"] = 9] = "Time";
      Type2[Type2["Timestamp"] = 10] = "Timestamp";
      Type2[Type2["Interval"] = 11] = "Interval";
      Type2[Type2["List"] = 12] = "List";
      Type2[Type2["Struct"] = 13] = "Struct";
      Type2[Type2["Union"] = 14] = "Union";
      Type2[Type2["FixedSizeBinary"] = 15] = "FixedSizeBinary";
      Type2[Type2["FixedSizeList"] = 16] = "FixedSizeList";
      Type2[Type2["Map"] = 17] = "Map";
      Type2[Type2["Duration"] = 18] = "Duration";
      Type2[Type2["LargeBinary"] = 19] = "LargeBinary";
      Type2[Type2["LargeUtf8"] = 20] = "LargeUtf8";
      Type2[Type2["Dictionary"] = -1] = "Dictionary";
      Type2[Type2["Int8"] = -2] = "Int8";
      Type2[Type2["Int16"] = -3] = "Int16";
      Type2[Type2["Int32"] = -4] = "Int32";
      Type2[Type2["Int64"] = -5] = "Int64";
      Type2[Type2["Uint8"] = -6] = "Uint8";
      Type2[Type2["Uint16"] = -7] = "Uint16";
      Type2[Type2["Uint32"] = -8] = "Uint32";
      Type2[Type2["Uint64"] = -9] = "Uint64";
      Type2[Type2["Float16"] = -10] = "Float16";
      Type2[Type2["Float32"] = -11] = "Float32";
      Type2[Type2["Float64"] = -12] = "Float64";
      Type2[Type2["DateDay"] = -13] = "DateDay";
      Type2[Type2["DateMillisecond"] = -14] = "DateMillisecond";
      Type2[Type2["TimestampSecond"] = -15] = "TimestampSecond";
      Type2[Type2["TimestampMillisecond"] = -16] = "TimestampMillisecond";
      Type2[Type2["TimestampMicrosecond"] = -17] = "TimestampMicrosecond";
      Type2[Type2["TimestampNanosecond"] = -18] = "TimestampNanosecond";
      Type2[Type2["TimeSecond"] = -19] = "TimeSecond";
      Type2[Type2["TimeMillisecond"] = -20] = "TimeMillisecond";
      Type2[Type2["TimeMicrosecond"] = -21] = "TimeMicrosecond";
      Type2[Type2["TimeNanosecond"] = -22] = "TimeNanosecond";
      Type2[Type2["DenseUnion"] = -23] = "DenseUnion";
      Type2[Type2["SparseUnion"] = -24] = "SparseUnion";
      Type2[Type2["IntervalDayTime"] = -25] = "IntervalDayTime";
      Type2[Type2["IntervalYearMonth"] = -26] = "IntervalYearMonth";
      Type2[Type2["DurationSecond"] = -27] = "DurationSecond";
      Type2[Type2["DurationMillisecond"] = -28] = "DurationMillisecond";
      Type2[Type2["DurationMicrosecond"] = -29] = "DurationMicrosecond";
      Type2[Type2["DurationNanosecond"] = -30] = "DurationNanosecond";
    })(Type || (exports2.Type = Type = {}));
    var BufferType;
    (function(BufferType2) {
      BufferType2[BufferType2["OFFSET"] = 0] = "OFFSET";
      BufferType2[BufferType2["DATA"] = 1] = "DATA";
      BufferType2[BufferType2["VALIDITY"] = 2] = "VALIDITY";
      BufferType2[BufferType2["TYPE"] = 3] = "TYPE";
    })(BufferType || (exports2.BufferType = BufferType = {}));
  }
});

// node_modules/apache-arrow/util/pretty.js
var require_pretty = __commonJS({
  "node_modules/apache-arrow/util/pretty.js"(exports2) {
    "use strict";
    Object.defineProperty(exports2, "__esModule", { value: true });
    exports2.valueToString = void 0;
    var undf = void 0;
    function valueToString(x) {
      if (x === null) {
        return "null";
      }
      if (x === undf) {
        return "undefined";
      }
      switch (typeof x) {
        case "number":
          return `${x}`;
        case "bigint":
          return `${x}`;
        case "string":
          return `"${x}"`;
      }
      if (typeof x[Symbol.toPrimitive] === "function") {
        return x[Symbol.toPrimitive]("string");
      }
      if (ArrayBuffer.isView(x)) {
        if (x instanceof BigInt64Array || x instanceof BigUint64Array) {
          return `[${[...x].map((x2) => valueToString(x2))}]`;
        }
        return `[${x}]`;
      }
      return ArrayBuffer.isView(x) ? `[${x}]` : JSON.stringify(x, (_, y) => typeof y === "bigint" ? `${y}` : y);
    }
    exports2.valueToString = valueToString;
  }
});

// node_modules/apache-arrow/util/bigint.js
var require_bigint = __commonJS({
  "node_modules/apache-arrow/util/bigint.js"(exports2) {
    "use strict";
    Object.defineProperty(exports2, "__esModule", { value: true });
    exports2.divideBigInts = exports2.bigIntToNumber = void 0;
    function bigIntToNumber(number) {
      if (typeof number === "bigint" && (number < Number.MIN_SAFE_INTEGER || number > Number.MAX_SAFE_INTEGER)) {
        throw new TypeError(`${number} is not safe to convert to a number.`);
      }
      return Number(number);
    }
    exports2.bigIntToNumber = bigIntToNumber;
    function divideBigInts(number, divisor) {
      return bigIntToNumber(number / divisor) + bigIntToNumber(number % divisor) / bigIntToNumber(divisor);
    }
    exports2.divideBigInts = divideBigInts;
  }
});

// node_modules/apache-arrow/util/bn.js
var require_bn = __commonJS({
  "node_modules/apache-arrow/util/bn.js"(exports2) {
    "use strict";
    Object.defineProperty(exports2, "__esModule", { value: true });
    exports2.BN = exports2.bigNumToBigInt = exports2.bigNumToString = exports2.bigNumToNumber = exports2.isArrowBigNumSymbol = void 0;
    var buffer_js_1 = require_buffer();
    var bigint_js_1 = require_bigint();
    exports2.isArrowBigNumSymbol = /* @__PURE__ */ Symbol.for("isArrowBigNum");
    function BigNum(x, ...xs) {
      if (xs.length === 0) {
        return Object.setPrototypeOf((0, buffer_js_1.toArrayBufferView)(this["TypedArray"], x), this.constructor.prototype);
      }
      return Object.setPrototypeOf(new this["TypedArray"](x, ...xs), this.constructor.prototype);
    }
    BigNum.prototype[exports2.isArrowBigNumSymbol] = true;
    BigNum.prototype.toJSON = function() {
      return `"${bigNumToString(this)}"`;
    };
    BigNum.prototype.valueOf = function(scale) {
      return bigNumToNumber(this, scale);
    };
    BigNum.prototype.toString = function() {
      return bigNumToString(this);
    };
    BigNum.prototype[Symbol.toPrimitive] = function(hint = "default") {
      switch (hint) {
        case "number":
          return bigNumToNumber(this);
        case "string":
          return bigNumToString(this);
        case "default":
          return bigNumToBigInt(this);
      }
      return bigNumToString(this);
    };
    function SignedBigNum(...args) {
      return BigNum.apply(this, args);
    }
    function UnsignedBigNum(...args) {
      return BigNum.apply(this, args);
    }
    function DecimalBigNum(...args) {
      return BigNum.apply(this, args);
    }
    Object.setPrototypeOf(SignedBigNum.prototype, Object.create(Int32Array.prototype));
    Object.setPrototypeOf(UnsignedBigNum.prototype, Object.create(Uint32Array.prototype));
    Object.setPrototypeOf(DecimalBigNum.prototype, Object.create(Uint32Array.prototype));
    Object.assign(SignedBigNum.prototype, BigNum.prototype, { "constructor": SignedBigNum, "signed": true, "TypedArray": Int32Array, "BigIntArray": BigInt64Array });
    Object.assign(UnsignedBigNum.prototype, BigNum.prototype, { "constructor": UnsignedBigNum, "signed": false, "TypedArray": Uint32Array, "BigIntArray": BigUint64Array });
    Object.assign(DecimalBigNum.prototype, BigNum.prototype, { "constructor": DecimalBigNum, "signed": true, "TypedArray": Uint32Array, "BigIntArray": BigUint64Array });
    var TWO_TO_THE_64 = BigInt(4294967296) * BigInt(4294967296);
    var TWO_TO_THE_64_MINUS_1 = TWO_TO_THE_64 - BigInt(1);
    function bigNumToNumber(bn, scale) {
      const { buffer, byteOffset, byteLength, "signed": signed } = bn;
      const words = new BigUint64Array(buffer, byteOffset, byteLength / 8);
      const negative = signed && words.at(-1) & BigInt(1) << BigInt(63);
      let number = BigInt(0);
      let i = 0;
      if (negative) {
        for (const word of words) {
          number |= (word ^ TWO_TO_THE_64_MINUS_1) * (BigInt(1) << BigInt(64 * i++));
        }
        number *= BigInt(-1);
        number -= BigInt(1);
      } else {
        for (const word of words) {
          number |= word * (BigInt(1) << BigInt(64 * i++));
        }
      }
      if (typeof scale === "number") {
        const denominator = BigInt(Math.pow(10, scale));
        const quotient = number / denominator;
        const remainder = number % denominator;
        return (0, bigint_js_1.bigIntToNumber)(quotient) + (0, bigint_js_1.bigIntToNumber)(remainder) / (0, bigint_js_1.bigIntToNumber)(denominator);
      }
      return (0, bigint_js_1.bigIntToNumber)(number);
    }
    exports2.bigNumToNumber = bigNumToNumber;
    function bigNumToString(a) {
      if (a.byteLength === 8) {
        const bigIntArray = new a["BigIntArray"](a.buffer, a.byteOffset, 1);
        return `${bigIntArray[0]}`;
      }
      if (!a["signed"]) {
        return unsignedBigNumToString(a);
      }
      let array = new Uint16Array(a.buffer, a.byteOffset, a.byteLength / 2);
      const highOrderWord = new Int16Array([array.at(-1)])[0];
      if (highOrderWord >= 0) {
        return unsignedBigNumToString(a);
      }
      array = array.slice();
      let carry = 1;
      for (let i = 0; i < array.length; i++) {
        const elem = array[i];
        const updated = ~elem + carry;
        array[i] = updated;
        carry &= elem === 0 ? 1 : 0;
      }
      const negated = unsignedBigNumToString(array);
      return `-${negated}`;
    }
    exports2.bigNumToString = bigNumToString;
    function bigNumToBigInt(a) {
      if (a.byteLength === 8) {
        const bigIntArray = new a["BigIntArray"](a.buffer, a.byteOffset, 1);
        return bigIntArray[0];
      } else {
        return bigNumToString(a);
      }
    }
    exports2.bigNumToBigInt = bigNumToBigInt;
    function unsignedBigNumToString(a) {
      let digits = "";
      const base64 = new Uint32Array(2);
      let base32 = new Uint16Array(a.buffer, a.byteOffset, a.byteLength / 2);
      const checks = new Uint32Array((base32 = new Uint16Array(base32).reverse()).buffer);
      let i = -1;
      const n = base32.length - 1;
      do {
        for (base64[0] = base32[i = 0]; i < n; ) {
          base32[i++] = base64[1] = base64[0] / 10;
          base64[0] = (base64[0] - base64[1] * 10 << 16) + base32[i];
        }
        base32[i] = base64[1] = base64[0] / 10;
        base64[0] = base64[0] - base64[1] * 10;
        digits = `${base64[0]}${digits}`;
      } while (checks[0] || checks[1] || checks[2] || checks[3]);
      return digits !== null && digits !== void 0 ? digits : `0`;
    }
    var BN = class _BN {
      /** @nocollapse */
      static new(num, isSigned) {
        switch (isSigned) {
          case true:
            return new SignedBigNum(num);
          case false:
            return new UnsignedBigNum(num);
        }
        switch (num.constructor) {
          case Int8Array:
          case Int16Array:
          case Int32Array:
          case BigInt64Array:
            return new SignedBigNum(num);
        }
        if (num.byteLength === 16) {
          return new DecimalBigNum(num);
        }
        return new UnsignedBigNum(num);
      }
      /** @nocollapse */
      static signed(num) {
        return new SignedBigNum(num);
      }
      /** @nocollapse */
      static unsigned(num) {
        return new UnsignedBigNum(num);
      }
      /** @nocollapse */
      static decimal(num) {
        return new DecimalBigNum(num);
      }
      constructor(num, isSigned) {
        return _BN.new(num, isSigned);
      }
    };
    exports2.BN = BN;
  }
});

// node_modules/apache-arrow/type.js
var require_type2 = __commonJS({
  "node_modules/apache-arrow/type.js"(exports2) {
    "use strict";
    var _a;
    var _b;
    var _c;
    var _d;
    var _e;
    var _f;
    var _g;
    var _h;
    var _j;
    var _k;
    var _l;
    var _m;
    var _o;
    var _p;
    var _q;
    var _r;
    var _s;
    var _t;
    var _u;
    var _v;
    var _w;
    var _x;
    Object.defineProperty(exports2, "__esModule", { value: true });
    exports2.Map_ = exports2.FixedSizeList = exports2.FixedSizeBinary = exports2.SparseUnion = exports2.DenseUnion = exports2.Union = exports2.Struct = exports2.List = exports2.DurationNanosecond = exports2.DurationMicrosecond = exports2.DurationMillisecond = exports2.DurationSecond = exports2.Duration = exports2.IntervalYearMonth = exports2.IntervalDayTime = exports2.Interval = exports2.TimestampNanosecond = exports2.TimestampMicrosecond = exports2.TimestampMillisecond = exports2.TimestampSecond = exports2.Timestamp = exports2.TimeNanosecond = exports2.TimeMicrosecond = exports2.TimeMillisecond = exports2.TimeSecond = exports2.Time = exports2.DateMillisecond = exports2.DateDay = exports2.Date_ = exports2.Decimal = exports2.Bool = exports2.LargeUtf8 = exports2.Utf8 = exports2.LargeBinary = exports2.Binary = exports2.Float64 = exports2.Float32 = exports2.Float16 = exports2.Float = exports2.Uint64 = exports2.Uint32 = exports2.Uint16 = exports2.Uint8 = exports2.Int64 = exports2.Int32 = exports2.Int16 = exports2.Int8 = exports2.Int = exports2.Null = exports2.DataType = void 0;
    exports2.strideForType = exports2.Dictionary = void 0;
    var bigint_js_1 = require_bigint();
    var enum_js_1 = require_enum();
    var DataType = class _DataType {
      /** @nocollapse */
      static isNull(x) {
        return (x === null || x === void 0 ? void 0 : x.typeId) === enum_js_1.Type.Null;
      }
      /** @nocollapse */
      static isInt(x) {
        return (x === null || x === void 0 ? void 0 : x.typeId) === enum_js_1.Type.Int;
      }
      /** @nocollapse */
      static isFloat(x) {
        return (x === null || x === void 0 ? void 0 : x.typeId) === enum_js_1.Type.Float;
      }
      /** @nocollapse */
      static isBinary(x) {
        return (x === null || x === void 0 ? void 0 : x.typeId) === enum_js_1.Type.Binary;
      }
      /** @nocollapse */
      static isLargeBinary(x) {
        return (x === null || x === void 0 ? void 0 : x.typeId) === enum_js_1.Type.LargeBinary;
      }
      /** @nocollapse */
      static isUtf8(x) {
        return (x === null || x === void 0 ? void 0 : x.typeId) === enum_js_1.Type.Utf8;
      }
      /** @nocollapse */
      static isLargeUtf8(x) {
        return (x === null || x === void 0 ? void 0 : x.typeId) === enum_js_1.Type.LargeUtf8;
      }
      /** @nocollapse */
      static isBool(x) {
        return (x === null || x === void 0 ? void 0 : x.typeId) === enum_js_1.Type.Bool;
      }
      /** @nocollapse */
      static isDecimal(x) {
        return (x === null || x === void 0 ? void 0 : x.typeId) === enum_js_1.Type.Decimal;
      }
      /** @nocollapse */
      static isDate(x) {
        return (x === null || x === void 0 ? void 0 : x.typeId) === enum_js_1.Type.Date;
      }
      /** @nocollapse */
      static isTime(x) {
        return (x === null || x === void 0 ? void 0 : x.typeId) === enum_js_1.Type.Time;
      }
      /** @nocollapse */
      static isTimestamp(x) {
        return (x === null || x === void 0 ? void 0 : x.typeId) === enum_js_1.Type.Timestamp;
      }
      /** @nocollapse */
      static isInterval(x) {
        return (x === null || x === void 0 ? void 0 : x.typeId) === enum_js_1.Type.Interval;
      }
      /** @nocollapse */
      static isDuration(x) {
        return (x === null || x === void 0 ? void 0 : x.typeId) === enum_js_1.Type.Duration;
      }
      /** @nocollapse */
      static isList(x) {
        return (x === null || x === void 0 ? void 0 : x.typeId) === enum_js_1.Type.List;
      }
      /** @nocollapse */
      static isStruct(x) {
        return (x === null || x === void 0 ? void 0 : x.typeId) === enum_js_1.Type.Struct;
      }
      /** @nocollapse */
      static isUnion(x) {
        return (x === null || x === void 0 ? void 0 : x.typeId) === enum_js_1.Type.Union;
      }
      /** @nocollapse */
      static isFixedSizeBinary(x) {
        return (x === null || x === void 0 ? void 0 : x.typeId) === enum_js_1.Type.FixedSizeBinary;
      }
      /** @nocollapse */
      static isFixedSizeList(x) {
        return (x === null || x === void 0 ? void 0 : x.typeId) === enum_js_1.Type.FixedSizeList;
      }
      /** @nocollapse */
      static isMap(x) {
        return (x === null || x === void 0 ? void 0 : x.typeId) === enum_js_1.Type.Map;
      }
      /** @nocollapse */
      static isDictionary(x) {
        return (x === null || x === void 0 ? void 0 : x.typeId) === enum_js_1.Type.Dictionary;
      }
      /** @nocollapse */
      static isDenseUnion(x) {
        return _DataType.isUnion(x) && x.mode === enum_js_1.UnionMode.Dense;
      }
      /** @nocollapse */
      static isSparseUnion(x) {
        return _DataType.isUnion(x) && x.mode === enum_js_1.UnionMode.Sparse;
      }
      constructor(typeId) {
        this.typeId = typeId;
      }
    };
    exports2.DataType = DataType;
    _a = Symbol.toStringTag;
    DataType[_a] = ((proto) => {
      proto.children = null;
      proto.ArrayType = Array;
      proto.OffsetArrayType = Int32Array;
      return proto[Symbol.toStringTag] = "DataType";
    })(DataType.prototype);
    var Null = class extends DataType {
      constructor() {
        super(enum_js_1.Type.Null);
      }
      toString() {
        return `Null`;
      }
    };
    exports2.Null = Null;
    _b = Symbol.toStringTag;
    Null[_b] = ((proto) => proto[Symbol.toStringTag] = "Null")(Null.prototype);
    var Int_ = class extends DataType {
      constructor(isSigned, bitWidth) {
        super(enum_js_1.Type.Int);
        this.isSigned = isSigned;
        this.bitWidth = bitWidth;
      }
      get ArrayType() {
        switch (this.bitWidth) {
          case 8:
            return this.isSigned ? Int8Array : Uint8Array;
          case 16:
            return this.isSigned ? Int16Array : Uint16Array;
          case 32:
            return this.isSigned ? Int32Array : Uint32Array;
          case 64:
            return this.isSigned ? BigInt64Array : BigUint64Array;
        }
        throw new Error(`Unrecognized ${this[Symbol.toStringTag]} type`);
      }
      toString() {
        return `${this.isSigned ? `I` : `Ui`}nt${this.bitWidth}`;
      }
    };
    exports2.Int = Int_;
    _c = Symbol.toStringTag;
    Int_[_c] = ((proto) => {
      proto.isSigned = null;
      proto.bitWidth = null;
      return proto[Symbol.toStringTag] = "Int";
    })(Int_.prototype);
    var Int8 = class extends Int_ {
      constructor() {
        super(true, 8);
      }
      get ArrayType() {
        return Int8Array;
      }
    };
    exports2.Int8 = Int8;
    var Int16 = class extends Int_ {
      constructor() {
        super(true, 16);
      }
      get ArrayType() {
        return Int16Array;
      }
    };
    exports2.Int16 = Int16;
    var Int32 = class extends Int_ {
      constructor() {
        super(true, 32);
      }
      get ArrayType() {
        return Int32Array;
      }
    };
    exports2.Int32 = Int32;
    var Int64 = class extends Int_ {
      constructor() {
        super(true, 64);
      }
      get ArrayType() {
        return BigInt64Array;
      }
    };
    exports2.Int64 = Int64;
    var Uint8 = class extends Int_ {
      constructor() {
        super(false, 8);
      }
      get ArrayType() {
        return Uint8Array;
      }
    };
    exports2.Uint8 = Uint8;
    var Uint16 = class extends Int_ {
      constructor() {
        super(false, 16);
      }
      get ArrayType() {
        return Uint16Array;
      }
    };
    exports2.Uint16 = Uint16;
    var Uint32 = class extends Int_ {
      constructor() {
        super(false, 32);
      }
      get ArrayType() {
        return Uint32Array;
      }
    };
    exports2.Uint32 = Uint32;
    var Uint64 = class extends Int_ {
      constructor() {
        super(false, 64);
      }
      get ArrayType() {
        return BigUint64Array;
      }
    };
    exports2.Uint64 = Uint64;
    Object.defineProperty(Int8.prototype, "ArrayType", { value: Int8Array });
    Object.defineProperty(Int16.prototype, "ArrayType", { value: Int16Array });
    Object.defineProperty(Int32.prototype, "ArrayType", { value: Int32Array });
    Object.defineProperty(Int64.prototype, "ArrayType", { value: BigInt64Array });
    Object.defineProperty(Uint8.prototype, "ArrayType", { value: Uint8Array });
    Object.defineProperty(Uint16.prototype, "ArrayType", { value: Uint16Array });
    Object.defineProperty(Uint32.prototype, "ArrayType", { value: Uint32Array });
    Object.defineProperty(Uint64.prototype, "ArrayType", { value: BigUint64Array });
    var Float = class extends DataType {
      constructor(precision) {
        super(enum_js_1.Type.Float);
        this.precision = precision;
      }
      get ArrayType() {
        switch (this.precision) {
          case enum_js_1.Precision.HALF:
            return Uint16Array;
          case enum_js_1.Precision.SINGLE:
            return Float32Array;
          case enum_js_1.Precision.DOUBLE:
            return Float64Array;
        }
        throw new Error(`Unrecognized ${this[Symbol.toStringTag]} type`);
      }
      toString() {
        return `Float${this.precision << 5 || 16}`;
      }
    };
    exports2.Float = Float;
    _d = Symbol.toStringTag;
    Float[_d] = ((proto) => {
      proto.precision = null;
      return proto[Symbol.toStringTag] = "Float";
    })(Float.prototype);
    var Float16 = class extends Float {
      constructor() {
        super(enum_js_1.Precision.HALF);
      }
    };
    exports2.Float16 = Float16;
    var Float32 = class extends Float {
      constructor() {
        super(enum_js_1.Precision.SINGLE);
      }
    };
    exports2.Float32 = Float32;
    var Float64 = class extends Float {
      constructor() {
        super(enum_js_1.Precision.DOUBLE);
      }
    };
    exports2.Float64 = Float64;
    Object.defineProperty(Float16.prototype, "ArrayType", { value: Uint16Array });
    Object.defineProperty(Float32.prototype, "ArrayType", { value: Float32Array });
    Object.defineProperty(Float64.prototype, "ArrayType", { value: Float64Array });
    var Binary = class extends DataType {
      constructor() {
        super(enum_js_1.Type.Binary);
      }
      toString() {
        return `Binary`;
      }
    };
    exports2.Binary = Binary;
    _e = Symbol.toStringTag;
    Binary[_e] = ((proto) => {
      proto.ArrayType = Uint8Array;
      return proto[Symbol.toStringTag] = "Binary";
    })(Binary.prototype);
    var LargeBinary = class extends DataType {
      constructor() {
        super(enum_js_1.Type.LargeBinary);
      }
      toString() {
        return `LargeBinary`;
      }
    };
    exports2.LargeBinary = LargeBinary;
    _f = Symbol.toStringTag;
    LargeBinary[_f] = ((proto) => {
      proto.ArrayType = Uint8Array;
      proto.OffsetArrayType = BigInt64Array;
      return proto[Symbol.toStringTag] = "LargeBinary";
    })(LargeBinary.prototype);
    var Utf8 = class extends DataType {
      constructor() {
        super(enum_js_1.Type.Utf8);
      }
      toString() {
        return `Utf8`;
      }
    };
    exports2.Utf8 = Utf8;
    _g = Symbol.toStringTag;
    Utf8[_g] = ((proto) => {
      proto.ArrayType = Uint8Array;
      return proto[Symbol.toStringTag] = "Utf8";
    })(Utf8.prototype);
    var LargeUtf8 = class extends DataType {
      constructor() {
        super(enum_js_1.Type.LargeUtf8);
      }
      toString() {
        return `LargeUtf8`;
      }
    };
    exports2.LargeUtf8 = LargeUtf8;
    _h = Symbol.toStringTag;
    LargeUtf8[_h] = ((proto) => {
      proto.ArrayType = Uint8Array;
      proto.OffsetArrayType = BigInt64Array;
      return proto[Symbol.toStringTag] = "LargeUtf8";
    })(LargeUtf8.prototype);
    var Bool = class extends DataType {
      constructor() {
        super(enum_js_1.Type.Bool);
      }
      toString() {
        return `Bool`;
      }
    };
    exports2.Bool = Bool;
    _j = Symbol.toStringTag;
    Bool[_j] = ((proto) => {
      proto.ArrayType = Uint8Array;
      return proto[Symbol.toStringTag] = "Bool";
    })(Bool.prototype);
    var Decimal = class extends DataType {
      constructor(scale, precision, bitWidth = 128) {
        super(enum_js_1.Type.Decimal);
        this.scale = scale;
        this.precision = precision;
        this.bitWidth = bitWidth;
      }
      toString() {
        return `Decimal[${this.precision}e${this.scale > 0 ? `+` : ``}${this.scale}]`;
      }
    };
    exports2.Decimal = Decimal;
    _k = Symbol.toStringTag;
    Decimal[_k] = ((proto) => {
      proto.scale = null;
      proto.precision = null;
      proto.ArrayType = Uint32Array;
      return proto[Symbol.toStringTag] = "Decimal";
    })(Decimal.prototype);
    var Date_ = class extends DataType {
      constructor(unit) {
        super(enum_js_1.Type.Date);
        this.unit = unit;
      }
      toString() {
        return `Date${(this.unit + 1) * 32}<${enum_js_1.DateUnit[this.unit]}>`;
      }
      get ArrayType() {
        return this.unit === enum_js_1.DateUnit.DAY ? Int32Array : BigInt64Array;
      }
    };
    exports2.Date_ = Date_;
    _l = Symbol.toStringTag;
    Date_[_l] = ((proto) => {
      proto.unit = null;
      return proto[Symbol.toStringTag] = "Date";
    })(Date_.prototype);
    var DateDay = class extends Date_ {
      constructor() {
        super(enum_js_1.DateUnit.DAY);
      }
    };
    exports2.DateDay = DateDay;
    var DateMillisecond = class extends Date_ {
      constructor() {
        super(enum_js_1.DateUnit.MILLISECOND);
      }
    };
    exports2.DateMillisecond = DateMillisecond;
    var Time_ = class extends DataType {
      constructor(unit, bitWidth) {
        super(enum_js_1.Type.Time);
        this.unit = unit;
        this.bitWidth = bitWidth;
      }
      toString() {
        return `Time${this.bitWidth}<${enum_js_1.TimeUnit[this.unit]}>`;
      }
      get ArrayType() {
        switch (this.bitWidth) {
          case 32:
            return Int32Array;
          case 64:
            return BigInt64Array;
        }
        throw new Error(`Unrecognized ${this[Symbol.toStringTag]} type`);
      }
    };
    exports2.Time = Time_;
    _m = Symbol.toStringTag;
    Time_[_m] = ((proto) => {
      proto.unit = null;
      proto.bitWidth = null;
      return proto[Symbol.toStringTag] = "Time";
    })(Time_.prototype);
    var TimeSecond = class extends Time_ {
      constructor() {
        super(enum_js_1.TimeUnit.SECOND, 32);
      }
    };
    exports2.TimeSecond = TimeSecond;
    var TimeMillisecond = class extends Time_ {
      constructor() {
        super(enum_js_1.TimeUnit.MILLISECOND, 32);
      }
    };
    exports2.TimeMillisecond = TimeMillisecond;
    var TimeMicrosecond = class extends Time_ {
      constructor() {
        super(enum_js_1.TimeUnit.MICROSECOND, 64);
      }
    };
    exports2.TimeMicrosecond = TimeMicrosecond;
    var TimeNanosecond = class extends Time_ {
      constructor() {
        super(enum_js_1.TimeUnit.NANOSECOND, 64);
      }
    };
    exports2.TimeNanosecond = TimeNanosecond;
    var Timestamp_ = class extends DataType {
      constructor(unit, timezone) {
        super(enum_js_1.Type.Timestamp);
        this.unit = unit;
        this.timezone = timezone;
      }
      toString() {
        return `Timestamp<${enum_js_1.TimeUnit[this.unit]}${this.timezone ? `, ${this.timezone}` : ``}>`;
      }
    };
    exports2.Timestamp = Timestamp_;
    _o = Symbol.toStringTag;
    Timestamp_[_o] = ((proto) => {
      proto.unit = null;
      proto.timezone = null;
      proto.ArrayType = BigInt64Array;
      return proto[Symbol.toStringTag] = "Timestamp";
    })(Timestamp_.prototype);
    var TimestampSecond = class extends Timestamp_ {
      constructor(timezone) {
        super(enum_js_1.TimeUnit.SECOND, timezone);
      }
    };
    exports2.TimestampSecond = TimestampSecond;
    var TimestampMillisecond = class extends Timestamp_ {
      constructor(timezone) {
        super(enum_js_1.TimeUnit.MILLISECOND, timezone);
      }
    };
    exports2.TimestampMillisecond = TimestampMillisecond;
    var TimestampMicrosecond = class extends Timestamp_ {
      constructor(timezone) {
        super(enum_js_1.TimeUnit.MICROSECOND, timezone);
      }
    };
    exports2.TimestampMicrosecond = TimestampMicrosecond;
    var TimestampNanosecond = class extends Timestamp_ {
      constructor(timezone) {
        super(enum_js_1.TimeUnit.NANOSECOND, timezone);
      }
    };
    exports2.TimestampNanosecond = TimestampNanosecond;
    var Interval_ = class extends DataType {
      constructor(unit) {
        super(enum_js_1.Type.Interval);
        this.unit = unit;
      }
      toString() {
        return `Interval<${enum_js_1.IntervalUnit[this.unit]}>`;
      }
    };
    exports2.Interval = Interval_;
    _p = Symbol.toStringTag;
    Interval_[_p] = ((proto) => {
      proto.unit = null;
      proto.ArrayType = Int32Array;
      return proto[Symbol.toStringTag] = "Interval";
    })(Interval_.prototype);
    var IntervalDayTime = class extends Interval_ {
      constructor() {
        super(enum_js_1.IntervalUnit.DAY_TIME);
      }
    };
    exports2.IntervalDayTime = IntervalDayTime;
    var IntervalYearMonth = class extends Interval_ {
      constructor() {
        super(enum_js_1.IntervalUnit.YEAR_MONTH);
      }
    };
    exports2.IntervalYearMonth = IntervalYearMonth;
    var Duration = class extends DataType {
      constructor(unit) {
        super(enum_js_1.Type.Duration);
        this.unit = unit;
      }
      toString() {
        return `Duration<${enum_js_1.TimeUnit[this.unit]}>`;
      }
    };
    exports2.Duration = Duration;
    _q = Symbol.toStringTag;
    Duration[_q] = ((proto) => {
      proto.unit = null;
      proto.ArrayType = BigInt64Array;
      return proto[Symbol.toStringTag] = "Duration";
    })(Duration.prototype);
    var DurationSecond = class extends Duration {
      constructor() {
        super(enum_js_1.TimeUnit.SECOND);
      }
    };
    exports2.DurationSecond = DurationSecond;
    var DurationMillisecond = class extends Duration {
      constructor() {
        super(enum_js_1.TimeUnit.MILLISECOND);
      }
    };
    exports2.DurationMillisecond = DurationMillisecond;
    var DurationMicrosecond = class extends Duration {
      constructor() {
        super(enum_js_1.TimeUnit.MICROSECOND);
      }
    };
    exports2.DurationMicrosecond = DurationMicrosecond;
    var DurationNanosecond = class extends Duration {
      constructor() {
        super(enum_js_1.TimeUnit.NANOSECOND);
      }
    };
    exports2.DurationNanosecond = DurationNanosecond;
    var List = class extends DataType {
      constructor(child) {
        super(enum_js_1.Type.List);
        this.children = [child];
      }
      toString() {
        return `List<${this.valueType}>`;
      }
      get valueType() {
        return this.children[0].type;
      }
      get valueField() {
        return this.children[0];
      }
      get ArrayType() {
        return this.valueType.ArrayType;
      }
    };
    exports2.List = List;
    _r = Symbol.toStringTag;
    List[_r] = ((proto) => {
      proto.children = null;
      return proto[Symbol.toStringTag] = "List";
    })(List.prototype);
    var Struct = class extends DataType {
      constructor(children) {
        super(enum_js_1.Type.Struct);
        this.children = children;
      }
      toString() {
        return `Struct<{${this.children.map((f) => `${f.name}:${f.type}`).join(`, `)}}>`;
      }
    };
    exports2.Struct = Struct;
    _s = Symbol.toStringTag;
    Struct[_s] = ((proto) => {
      proto.children = null;
      return proto[Symbol.toStringTag] = "Struct";
    })(Struct.prototype);
    var Union_ = class extends DataType {
      constructor(mode, typeIds, children) {
        super(enum_js_1.Type.Union);
        this.mode = mode;
        this.children = children;
        this.typeIds = typeIds = Int32Array.from(typeIds);
        this.typeIdToChildIndex = typeIds.reduce((typeIdToChildIndex, typeId, idx) => (typeIdToChildIndex[typeId] = idx) && typeIdToChildIndex || typeIdToChildIndex, /* @__PURE__ */ Object.create(null));
      }
      toString() {
        return `${this[Symbol.toStringTag]}<${this.children.map((x) => `${x.type}`).join(` | `)}>`;
      }
    };
    exports2.Union = Union_;
    _t = Symbol.toStringTag;
    Union_[_t] = ((proto) => {
      proto.mode = null;
      proto.typeIds = null;
      proto.children = null;
      proto.typeIdToChildIndex = null;
      proto.ArrayType = Int8Array;
      return proto[Symbol.toStringTag] = "Union";
    })(Union_.prototype);
    var DenseUnion = class extends Union_ {
      constructor(typeIds, children) {
        super(enum_js_1.UnionMode.Dense, typeIds, children);
      }
    };
    exports2.DenseUnion = DenseUnion;
    var SparseUnion = class extends Union_ {
      constructor(typeIds, children) {
        super(enum_js_1.UnionMode.Sparse, typeIds, children);
      }
    };
    exports2.SparseUnion = SparseUnion;
    var FixedSizeBinary = class extends DataType {
      constructor(byteWidth) {
        super(enum_js_1.Type.FixedSizeBinary);
        this.byteWidth = byteWidth;
      }
      toString() {
        return `FixedSizeBinary[${this.byteWidth}]`;
      }
    };
    exports2.FixedSizeBinary = FixedSizeBinary;
    _u = Symbol.toStringTag;
    FixedSizeBinary[_u] = ((proto) => {
      proto.byteWidth = null;
      proto.ArrayType = Uint8Array;
      return proto[Symbol.toStringTag] = "FixedSizeBinary";
    })(FixedSizeBinary.prototype);
    var FixedSizeList = class extends DataType {
      constructor(listSize, child) {
        super(enum_js_1.Type.FixedSizeList);
        this.listSize = listSize;
        this.children = [child];
      }
      get valueType() {
        return this.children[0].type;
      }
      get valueField() {
        return this.children[0];
      }
      get ArrayType() {
        return this.valueType.ArrayType;
      }
      toString() {
        return `FixedSizeList[${this.listSize}]<${this.valueType}>`;
      }
    };
    exports2.FixedSizeList = FixedSizeList;
    _v = Symbol.toStringTag;
    FixedSizeList[_v] = ((proto) => {
      proto.children = null;
      proto.listSize = null;
      return proto[Symbol.toStringTag] = "FixedSizeList";
    })(FixedSizeList.prototype);
    var Map_ = class extends DataType {
      constructor(entries, keysSorted = false) {
        var _y, _z, _0;
        super(enum_js_1.Type.Map);
        this.children = [entries];
        this.keysSorted = keysSorted;
        if (entries) {
          entries["name"] = "entries";
          if ((_y = entries === null || entries === void 0 ? void 0 : entries.type) === null || _y === void 0 ? void 0 : _y.children) {
            const key = (_z = entries === null || entries === void 0 ? void 0 : entries.type) === null || _z === void 0 ? void 0 : _z.children[0];
            if (key) {
              key["name"] = "key";
            }
            const val = (_0 = entries === null || entries === void 0 ? void 0 : entries.type) === null || _0 === void 0 ? void 0 : _0.children[1];
            if (val) {
              val["name"] = "value";
            }
          }
        }
      }
      get keyType() {
        return this.children[0].type.children[0].type;
      }
      get valueType() {
        return this.children[0].type.children[1].type;
      }
      get childType() {
        return this.children[0].type;
      }
      toString() {
        return `Map<{${this.children[0].type.children.map((f) => `${f.name}:${f.type}`).join(`, `)}}>`;
      }
    };
    exports2.Map_ = Map_;
    _w = Symbol.toStringTag;
    Map_[_w] = ((proto) => {
      proto.children = null;
      proto.keysSorted = null;
      return proto[Symbol.toStringTag] = "Map_";
    })(Map_.prototype);
    var getId = /* @__PURE__ */ ((atomicDictionaryId) => () => ++atomicDictionaryId)(-1);
    var Dictionary = class extends DataType {
      constructor(dictionary, indices, id, isOrdered) {
        super(enum_js_1.Type.Dictionary);
        this.indices = indices;
        this.dictionary = dictionary;
        this.isOrdered = isOrdered || false;
        this.id = id == null ? getId() : (0, bigint_js_1.bigIntToNumber)(id);
      }
      get children() {
        return this.dictionary.children;
      }
      get valueType() {
        return this.dictionary;
      }
      get ArrayType() {
        return this.dictionary.ArrayType;
      }
      toString() {
        return `Dictionary<${this.indices}, ${this.dictionary}>`;
      }
    };
    exports2.Dictionary = Dictionary;
    _x = Symbol.toStringTag;
    Dictionary[_x] = ((proto) => {
      proto.id = null;
      proto.indices = null;
      proto.isOrdered = null;
      proto.dictionary = null;
      return proto[Symbol.toStringTag] = "Dictionary";
    })(Dictionary.prototype);
    function strideForType(type) {
      const t = type;
      switch (type.typeId) {
        case enum_js_1.Type.Decimal:
          return type.bitWidth / 32;
        case enum_js_1.Type.Interval:
          return 1 + t.unit;
        // case Type.Int: return 1 + +((t as Int_).bitWidth > 32);
        // case Type.Time: return 1 + +((t as Time_).bitWidth > 32);
        case enum_js_1.Type.FixedSizeList:
          return t.listSize;
        case enum_js_1.Type.FixedSizeBinary:
          return t.byteWidth;
        default:
          return 1;
      }
    }
    exports2.strideForType = strideForType;
  }
});

// node_modules/apache-arrow/visitor.js
var require_visitor = __commonJS({
  "node_modules/apache-arrow/visitor.js"(exports2) {
    "use strict";
    Object.defineProperty(exports2, "__esModule", { value: true });
    exports2.Visitor = void 0;
    var enum_js_1 = require_enum();
    var type_js_1 = require_type2();
    var Visitor = class {
      visitMany(nodes, ...args) {
        return nodes.map((node, i) => this.visit(node, ...args.map((x) => x[i])));
      }
      visit(...args) {
        return this.getVisitFn(args[0], false).apply(this, args);
      }
      getVisitFn(node, throwIfNotFound = true) {
        return getVisitFn(this, node, throwIfNotFound);
      }
      getVisitFnByTypeId(typeId, throwIfNotFound = true) {
        return getVisitFnByTypeId(this, typeId, throwIfNotFound);
      }
      visitNull(_node, ..._args) {
        return null;
      }
      visitBool(_node, ..._args) {
        return null;
      }
      visitInt(_node, ..._args) {
        return null;
      }
      visitFloat(_node, ..._args) {
        return null;
      }
      visitUtf8(_node, ..._args) {
        return null;
      }
      visitLargeUtf8(_node, ..._args) {
        return null;
      }
      visitBinary(_node, ..._args) {
        return null;
      }
      visitLargeBinary(_node, ..._args) {
        return null;
      }
      visitFixedSizeBinary(_node, ..._args) {
        return null;
      }
      visitDate(_node, ..._args) {
        return null;
      }
      visitTimestamp(_node, ..._args) {
        return null;
      }
      visitTime(_node, ..._args) {
        return null;
      }
      visitDecimal(_node, ..._args) {
        return null;
      }
      visitList(_node, ..._args) {
        return null;
      }
      visitStruct(_node, ..._args) {
        return null;
      }
      visitUnion(_node, ..._args) {
        return null;
      }
      visitDictionary(_node, ..._args) {
        return null;
      }
      visitInterval(_node, ..._args) {
        return null;
      }
      visitDuration(_node, ..._args) {
        return null;
      }
      visitFixedSizeList(_node, ..._args) {
        return null;
      }
      visitMap(_node, ..._args) {
        return null;
      }
    };
    exports2.Visitor = Visitor;
    function getVisitFn(visitor, node, throwIfNotFound = true) {
      if (typeof node === "number") {
        return getVisitFnByTypeId(visitor, node, throwIfNotFound);
      }
      if (typeof node === "string" && node in enum_js_1.Type) {
        return getVisitFnByTypeId(visitor, enum_js_1.Type[node], throwIfNotFound);
      }
      if (node && node instanceof type_js_1.DataType) {
        return getVisitFnByTypeId(visitor, inferDType(node), throwIfNotFound);
      }
      if ((node === null || node === void 0 ? void 0 : node.type) && node.type instanceof type_js_1.DataType) {
        return getVisitFnByTypeId(visitor, inferDType(node.type), throwIfNotFound);
      }
      return getVisitFnByTypeId(visitor, enum_js_1.Type.NONE, throwIfNotFound);
    }
    function getVisitFnByTypeId(visitor, dtype, throwIfNotFound = true) {
      let fn = null;
      switch (dtype) {
        case enum_js_1.Type.Null:
          fn = visitor.visitNull;
          break;
        case enum_js_1.Type.Bool:
          fn = visitor.visitBool;
          break;
        case enum_js_1.Type.Int:
          fn = visitor.visitInt;
          break;
        case enum_js_1.Type.Int8:
          fn = visitor.visitInt8 || visitor.visitInt;
          break;
        case enum_js_1.Type.Int16:
          fn = visitor.visitInt16 || visitor.visitInt;
          break;
        case enum_js_1.Type.Int32:
          fn = visitor.visitInt32 || visitor.visitInt;
          break;
        case enum_js_1.Type.Int64:
          fn = visitor.visitInt64 || visitor.visitInt;
          break;
        case enum_js_1.Type.Uint8:
          fn = visitor.visitUint8 || visitor.visitInt;
          break;
        case enum_js_1.Type.Uint16:
          fn = visitor.visitUint16 || visitor.visitInt;
          break;
        case enum_js_1.Type.Uint32:
          fn = visitor.visitUint32 || visitor.visitInt;
          break;
        case enum_js_1.Type.Uint64:
          fn = visitor.visitUint64 || visitor.visitInt;
          break;
        case enum_js_1.Type.Float:
          fn = visitor.visitFloat;
          break;
        case enum_js_1.Type.Float16:
          fn = visitor.visitFloat16 || visitor.visitFloat;
          break;
        case enum_js_1.Type.Float32:
          fn = visitor.visitFloat32 || visitor.visitFloat;
          break;
        case enum_js_1.Type.Float64:
          fn = visitor.visitFloat64 || visitor.visitFloat;
          break;
        case enum_js_1.Type.Utf8:
          fn = visitor.visitUtf8;
          break;
        case enum_js_1.Type.LargeUtf8:
          fn = visitor.visitLargeUtf8;
          break;
        case enum_js_1.Type.Binary:
          fn = visitor.visitBinary;
          break;
        case enum_js_1.Type.LargeBinary:
          fn = visitor.visitLargeBinary;
          break;
        case enum_js_1.Type.FixedSizeBinary:
          fn = visitor.visitFixedSizeBinary;
          break;
        case enum_js_1.Type.Date:
          fn = visitor.visitDate;
          break;
        case enum_js_1.Type.DateDay:
          fn = visitor.visitDateDay || visitor.visitDate;
          break;
        case enum_js_1.Type.DateMillisecond:
          fn = visitor.visitDateMillisecond || visitor.visitDate;
          break;
        case enum_js_1.Type.Timestamp:
          fn = visitor.visitTimestamp;
          break;
        case enum_js_1.Type.TimestampSecond:
          fn = visitor.visitTimestampSecond || visitor.visitTimestamp;
          break;
        case enum_js_1.Type.TimestampMillisecond:
          fn = visitor.visitTimestampMillisecond || visitor.visitTimestamp;
          break;
        case enum_js_1.Type.TimestampMicrosecond:
          fn = visitor.visitTimestampMicrosecond || visitor.visitTimestamp;
          break;
        case enum_js_1.Type.TimestampNanosecond:
          fn = visitor.visitTimestampNanosecond || visitor.visitTimestamp;
          break;
        case enum_js_1.Type.Time:
          fn = visitor.visitTime;
          break;
        case enum_js_1.Type.TimeSecond:
          fn = visitor.visitTimeSecond || visitor.visitTime;
          break;
        case enum_js_1.Type.TimeMillisecond:
          fn = visitor.visitTimeMillisecond || visitor.visitTime;
          break;
        case enum_js_1.Type.TimeMicrosecond:
          fn = visitor.visitTimeMicrosecond || visitor.visitTime;
          break;
        case enum_js_1.Type.TimeNanosecond:
          fn = visitor.visitTimeNanosecond || visitor.visitTime;
          break;
        case enum_js_1.Type.Decimal:
          fn = visitor.visitDecimal;
          break;
        case enum_js_1.Type.List:
          fn = visitor.visitList;
          break;
        case enum_js_1.Type.Struct:
          fn = visitor.visitStruct;
          break;
        case enum_js_1.Type.Union:
          fn = visitor.visitUnion;
          break;
        case enum_js_1.Type.DenseUnion:
          fn = visitor.visitDenseUnion || visitor.visitUnion;
          break;
        case enum_js_1.Type.SparseUnion:
          fn = visitor.visitSparseUnion || visitor.visitUnion;
          break;
        case enum_js_1.Type.Dictionary:
          fn = visitor.visitDictionary;
          break;
        case enum_js_1.Type.Interval:
          fn = visitor.visitInterval;
          break;
        case enum_js_1.Type.IntervalDayTime:
          fn = visitor.visitIntervalDayTime || visitor.visitInterval;
          break;
        case enum_js_1.Type.IntervalYearMonth:
          fn = visitor.visitIntervalYearMonth || visitor.visitInterval;
          break;
        case enum_js_1.Type.Duration:
          fn = visitor.visitDuration;
          break;
        case enum_js_1.Type.DurationSecond:
          fn = visitor.visitDurationSecond || visitor.visitDuration;
          break;
        case enum_js_1.Type.DurationMillisecond:
          fn = visitor.visitDurationMillisecond || visitor.visitDuration;
          break;
        case enum_js_1.Type.DurationMicrosecond:
          fn = visitor.visitDurationMicrosecond || visitor.visitDuration;
          break;
        case enum_js_1.Type.DurationNanosecond:
          fn = visitor.visitDurationNanosecond || visitor.visitDuration;
          break;
        case enum_js_1.Type.FixedSizeList:
          fn = visitor.visitFixedSizeList;
          break;
        case enum_js_1.Type.Map:
          fn = visitor.visitMap;
          break;
      }
      if (typeof fn === "function")
        return fn;
      if (!throwIfNotFound)
        return () => null;
      throw new Error(`Unrecognized type '${enum_js_1.Type[dtype]}'`);
    }
    function inferDType(type) {
      switch (type.typeId) {
        case enum_js_1.Type.Null:
          return enum_js_1.Type.Null;
        case enum_js_1.Type.Int: {
          const { bitWidth, isSigned } = type;
          switch (bitWidth) {
            case 8:
              return isSigned ? enum_js_1.Type.Int8 : enum_js_1.Type.Uint8;
            case 16:
              return isSigned ? enum_js_1.Type.Int16 : enum_js_1.Type.Uint16;
            case 32:
              return isSigned ? enum_js_1.Type.Int32 : enum_js_1.Type.Uint32;
            case 64:
              return isSigned ? enum_js_1.Type.Int64 : enum_js_1.Type.Uint64;
          }
          return enum_js_1.Type.Int;
        }
        case enum_js_1.Type.Float:
          switch (type.precision) {
            case enum_js_1.Precision.HALF:
              return enum_js_1.Type.Float16;
            case enum_js_1.Precision.SINGLE:
              return enum_js_1.Type.Float32;
            case enum_js_1.Precision.DOUBLE:
              return enum_js_1.Type.Float64;
          }
          return enum_js_1.Type.Float;
        case enum_js_1.Type.Binary:
          return enum_js_1.Type.Binary;
        case enum_js_1.Type.LargeBinary:
          return enum_js_1.Type.LargeBinary;
        case enum_js_1.Type.Utf8:
          return enum_js_1.Type.Utf8;
        case enum_js_1.Type.LargeUtf8:
          return enum_js_1.Type.LargeUtf8;
        case enum_js_1.Type.Bool:
          return enum_js_1.Type.Bool;
        case enum_js_1.Type.Decimal:
          return enum_js_1.Type.Decimal;
        case enum_js_1.Type.Time:
          switch (type.unit) {
            case enum_js_1.TimeUnit.SECOND:
              return enum_js_1.Type.TimeSecond;
            case enum_js_1.TimeUnit.MILLISECOND:
              return enum_js_1.Type.TimeMillisecond;
            case enum_js_1.TimeUnit.MICROSECOND:
              return enum_js_1.Type.TimeMicrosecond;
            case enum_js_1.TimeUnit.NANOSECOND:
              return enum_js_1.Type.TimeNanosecond;
          }
          return enum_js_1.Type.Time;
        case enum_js_1.Type.Timestamp:
          switch (type.unit) {
            case enum_js_1.TimeUnit.SECOND:
              return enum_js_1.Type.TimestampSecond;
            case enum_js_1.TimeUnit.MILLISECOND:
              return enum_js_1.Type.TimestampMillisecond;
            case enum_js_1.TimeUnit.MICROSECOND:
              return enum_js_1.Type.TimestampMicrosecond;
            case enum_js_1.TimeUnit.NANOSECOND:
              return enum_js_1.Type.TimestampNanosecond;
          }
          return enum_js_1.Type.Timestamp;
        case enum_js_1.Type.Date:
          switch (type.unit) {
            case enum_js_1.DateUnit.DAY:
              return enum_js_1.Type.DateDay;
            case enum_js_1.DateUnit.MILLISECOND:
              return enum_js_1.Type.DateMillisecond;
          }
          return enum_js_1.Type.Date;
        case enum_js_1.Type.Interval:
          switch (type.unit) {
            case enum_js_1.IntervalUnit.DAY_TIME:
              return enum_js_1.Type.IntervalDayTime;
            case enum_js_1.IntervalUnit.YEAR_MONTH:
              return enum_js_1.Type.IntervalYearMonth;
          }
          return enum_js_1.Type.Interval;
        case enum_js_1.Type.Duration:
          switch (type.unit) {
            case enum_js_1.TimeUnit.SECOND:
              return enum_js_1.Type.DurationSecond;
            case enum_js_1.TimeUnit.MILLISECOND:
              return enum_js_1.Type.DurationMillisecond;
            case enum_js_1.TimeUnit.MICROSECOND:
              return enum_js_1.Type.DurationMicrosecond;
            case enum_js_1.TimeUnit.NANOSECOND:
              return enum_js_1.Type.DurationNanosecond;
          }
          return enum_js_1.Type.Duration;
        case enum_js_1.Type.Map:
          return enum_js_1.Type.Map;
        case enum_js_1.Type.List:
          return enum_js_1.Type.List;
        case enum_js_1.Type.Struct:
          return enum_js_1.Type.Struct;
        case enum_js_1.Type.Union:
          switch (type.mode) {
            case enum_js_1.UnionMode.Dense:
              return enum_js_1.Type.DenseUnion;
            case enum_js_1.UnionMode.Sparse:
              return enum_js_1.Type.SparseUnion;
          }
          return enum_js_1.Type.Union;
        case enum_js_1.Type.FixedSizeBinary:
          return enum_js_1.Type.FixedSizeBinary;
        case enum_js_1.Type.FixedSizeList:
          return enum_js_1.Type.FixedSizeList;
        case enum_js_1.Type.Dictionary:
          return enum_js_1.Type.Dictionary;
      }
      throw new Error(`Unrecognized type '${enum_js_1.Type[type.typeId]}'`);
    }
    Visitor.prototype.visitInt8 = null;
    Visitor.prototype.visitInt16 = null;
    Visitor.prototype.visitInt32 = null;
    Visitor.prototype.visitInt64 = null;
    Visitor.prototype.visitUint8 = null;
    Visitor.prototype.visitUint16 = null;
    Visitor.prototype.visitUint32 = null;
    Visitor.prototype.visitUint64 = null;
    Visitor.prototype.visitFloat16 = null;
    Visitor.prototype.visitFloat32 = null;
    Visitor.prototype.visitFloat64 = null;
    Visitor.prototype.visitDateDay = null;
    Visitor.prototype.visitDateMillisecond = null;
    Visitor.prototype.visitTimestampSecond = null;
    Visitor.prototype.visitTimestampMillisecond = null;
    Visitor.prototype.visitTimestampMicrosecond = null;
    Visitor.prototype.visitTimestampNanosecond = null;
    Visitor.prototype.visitTimeSecond = null;
    Visitor.prototype.visitTimeMillisecond = null;
    Visitor.prototype.visitTimeMicrosecond = null;
    Visitor.prototype.visitTimeNanosecond = null;
    Visitor.prototype.visitDenseUnion = null;
    Visitor.prototype.visitSparseUnion = null;
    Visitor.prototype.visitIntervalDayTime = null;
    Visitor.prototype.visitIntervalYearMonth = null;
    Visitor.prototype.visitDuration = null;
    Visitor.prototype.visitDurationSecond = null;
    Visitor.prototype.visitDurationMillisecond = null;
    Visitor.prototype.visitDurationMicrosecond = null;
    Visitor.prototype.visitDurationNanosecond = null;
  }
});

// node_modules/apache-arrow/util/math.js
var require_math = __commonJS({
  "node_modules/apache-arrow/util/math.js"(exports2) {
    "use strict";
    Object.defineProperty(exports2, "__esModule", { value: true });
    exports2.float64ToUint16 = exports2.uint16ToFloat64 = void 0;
    var f64 = new Float64Array(1);
    var u32 = new Uint32Array(f64.buffer);
    function uint16ToFloat64(h) {
      const expo = (h & 31744) >> 10;
      const sigf = (h & 1023) / 1024;
      const sign = Math.pow(-1, (h & 32768) >> 15);
      switch (expo) {
        case 31:
          return sign * (sigf ? Number.NaN : 1 / 0);
        case 0:
          return sign * (sigf ? 6103515625e-14 * sigf : 0);
      }
      return sign * Math.pow(2, expo - 15) * (1 + sigf);
    }
    exports2.uint16ToFloat64 = uint16ToFloat64;
    function float64ToUint16(d) {
      if (d !== d) {
        return 32256;
      }
      f64[0] = d;
      const sign = (u32[1] & 2147483648) >> 16 & 65535;
      let expo = u32[1] & 2146435072, sigf = 0;
      if (expo >= 1089470464) {
        if (u32[0] > 0) {
          expo = 31744;
        } else {
          expo = (expo & 2080374784) >> 16;
          sigf = (u32[1] & 1048575) >> 10;
        }
      } else if (expo <= 1056964608) {
        sigf = 1048576 + (u32[1] & 1048575);
        sigf = 1048576 + (sigf << (expo >> 20) - 998) >> 21;
        expo = 0;
      } else {
        expo = expo - 1056964608 >> 10;
        sigf = (u32[1] & 1048575) + 512 >> 10;
      }
      return sign | expo | sigf & 65535;
    }
    exports2.float64ToUint16 = float64ToUint16;
  }
});

// node_modules/apache-arrow/visitor/set.js
var require_set = __commonJS({
  "node_modules/apache-arrow/visitor/set.js"(exports2) {
    "use strict";
    Object.defineProperty(exports2, "__esModule", { value: true });
    exports2.instance = exports2.setDuration = exports2.setDurationNanosecond = exports2.setDurationMicrosecond = exports2.setDurationMillisecond = exports2.setDurationSecond = exports2.setIntervalYearMonth = exports2.setIntervalDayTime = exports2.setIntervalValue = exports2.setDecimal = exports2.setTime = exports2.setTimeNanosecond = exports2.setTimeMicrosecond = exports2.setTimeMillisecond = exports2.setTimeSecond = exports2.setTimestamp = exports2.setTimestampNanosecond = exports2.setTimestampMicrosecond = exports2.setTimestampMillisecond = exports2.setTimestampSecond = exports2.setDate = exports2.setFixedSizeBinary = exports2.setDateMillisecond = exports2.setDateDay = exports2.setAnyFloat = exports2.setFloat16 = exports2.setFloat = exports2.setInt = exports2.setVariableWidthBytes = exports2.setEpochMsToDays = exports2.SetVisitor = void 0;
    var vector_js_1 = require_vector2();
    var visitor_js_1 = require_visitor();
    var bigint_js_1 = require_bigint();
    var utf8_js_1 = require_utf8();
    var math_js_1 = require_math();
    var enum_js_1 = require_enum();
    var SetVisitor = class extends visitor_js_1.Visitor {
    };
    exports2.SetVisitor = SetVisitor;
    function wrapSet(fn) {
      return (data, _1, _2) => {
        if (data.setValid(_1, _2 != null)) {
          return fn(data, _1, _2);
        }
      };
    }
    var setEpochMsToDays = (data, index, epochMs) => {
      data[index] = Math.floor(epochMs / 864e5);
    };
    exports2.setEpochMsToDays = setEpochMsToDays;
    var setVariableWidthBytes = (values, valueOffsets, index, value) => {
      if (index + 1 < valueOffsets.length) {
        const x = (0, bigint_js_1.bigIntToNumber)(valueOffsets[index]);
        const y = (0, bigint_js_1.bigIntToNumber)(valueOffsets[index + 1]);
        values.set(value.subarray(0, y - x), x);
      }
    };
    exports2.setVariableWidthBytes = setVariableWidthBytes;
    var setBool = ({ offset, values }, index, val) => {
      const idx = offset + index;
      val ? values[idx >> 3] |= 1 << idx % 8 : values[idx >> 3] &= ~(1 << idx % 8);
    };
    var setInt = ({ values }, index, value) => {
      values[index] = value;
    };
    exports2.setInt = setInt;
    var setFloat = ({ values }, index, value) => {
      values[index] = value;
    };
    exports2.setFloat = setFloat;
    var setFloat16 = ({ values }, index, value) => {
      values[index] = (0, math_js_1.float64ToUint16)(value);
    };
    exports2.setFloat16 = setFloat16;
    var setAnyFloat = (data, index, value) => {
      switch (data.type.precision) {
        case enum_js_1.Precision.HALF:
          return (0, exports2.setFloat16)(data, index, value);
        case enum_js_1.Precision.SINGLE:
        case enum_js_1.Precision.DOUBLE:
          return (0, exports2.setFloat)(data, index, value);
      }
    };
    exports2.setAnyFloat = setAnyFloat;
    var setDateDay = ({ values }, index, value) => {
      (0, exports2.setEpochMsToDays)(values, index, value.valueOf());
    };
    exports2.setDateDay = setDateDay;
    var setDateMillisecond = ({ values }, index, value) => {
      values[index] = BigInt(value);
    };
    exports2.setDateMillisecond = setDateMillisecond;
    var setFixedSizeBinary = ({ stride, values }, index, value) => {
      values.set(value.subarray(0, stride), stride * index);
    };
    exports2.setFixedSizeBinary = setFixedSizeBinary;
    var setBinary = ({ values, valueOffsets }, index, value) => (0, exports2.setVariableWidthBytes)(values, valueOffsets, index, value);
    var setUtf8 = ({ values, valueOffsets }, index, value) => (0, exports2.setVariableWidthBytes)(values, valueOffsets, index, (0, utf8_js_1.encodeUtf8)(value));
    var setDate = (data, index, value) => {
      data.type.unit === enum_js_1.DateUnit.DAY ? (0, exports2.setDateDay)(data, index, value) : (0, exports2.setDateMillisecond)(data, index, value);
    };
    exports2.setDate = setDate;
    var setTimestampSecond = ({ values }, index, value) => {
      values[index] = BigInt(value / 1e3);
    };
    exports2.setTimestampSecond = setTimestampSecond;
    var setTimestampMillisecond = ({ values }, index, value) => {
      values[index] = BigInt(value);
    };
    exports2.setTimestampMillisecond = setTimestampMillisecond;
    var setTimestampMicrosecond = ({ values }, index, value) => {
      values[index] = BigInt(value * 1e3);
    };
    exports2.setTimestampMicrosecond = setTimestampMicrosecond;
    var setTimestampNanosecond = ({ values }, index, value) => {
      values[index] = BigInt(value * 1e6);
    };
    exports2.setTimestampNanosecond = setTimestampNanosecond;
    var setTimestamp = (data, index, value) => {
      switch (data.type.unit) {
        case enum_js_1.TimeUnit.SECOND:
          return (0, exports2.setTimestampSecond)(data, index, value);
        case enum_js_1.TimeUnit.MILLISECOND:
          return (0, exports2.setTimestampMillisecond)(data, index, value);
        case enum_js_1.TimeUnit.MICROSECOND:
          return (0, exports2.setTimestampMicrosecond)(data, index, value);
        case enum_js_1.TimeUnit.NANOSECOND:
          return (0, exports2.setTimestampNanosecond)(data, index, value);
      }
    };
    exports2.setTimestamp = setTimestamp;
    var setTimeSecond = ({ values }, index, value) => {
      values[index] = value;
    };
    exports2.setTimeSecond = setTimeSecond;
    var setTimeMillisecond = ({ values }, index, value) => {
      values[index] = value;
    };
    exports2.setTimeMillisecond = setTimeMillisecond;
    var setTimeMicrosecond = ({ values }, index, value) => {
      values[index] = value;
    };
    exports2.setTimeMicrosecond = setTimeMicrosecond;
    var setTimeNanosecond = ({ values }, index, value) => {
      values[index] = value;
    };
    exports2.setTimeNanosecond = setTimeNanosecond;
    var setTime = (data, index, value) => {
      switch (data.type.unit) {
        case enum_js_1.TimeUnit.SECOND:
          return (0, exports2.setTimeSecond)(data, index, value);
        case enum_js_1.TimeUnit.MILLISECOND:
          return (0, exports2.setTimeMillisecond)(data, index, value);
        case enum_js_1.TimeUnit.MICROSECOND:
          return (0, exports2.setTimeMicrosecond)(data, index, value);
        case enum_js_1.TimeUnit.NANOSECOND:
          return (0, exports2.setTimeNanosecond)(data, index, value);
      }
    };
    exports2.setTime = setTime;
    var setDecimal = ({ values, stride }, index, value) => {
      values.set(value.subarray(0, stride), stride * index);
    };
    exports2.setDecimal = setDecimal;
    var setList = (data, index, value) => {
      const values = data.children[0];
      const valueOffsets = data.valueOffsets;
      const set = exports2.instance.getVisitFn(values);
      if (Array.isArray(value)) {
        for (let idx = -1, itr = valueOffsets[index], end = valueOffsets[index + 1]; itr < end; ) {
          set(values, itr++, value[++idx]);
        }
      } else {
        for (let idx = -1, itr = valueOffsets[index], end = valueOffsets[index + 1]; itr < end; ) {
          set(values, itr++, value.get(++idx));
        }
      }
    };
    var setMap = (data, index, value) => {
      const values = data.children[0];
      const { valueOffsets } = data;
      const set = exports2.instance.getVisitFn(values);
      let { [index]: idx, [index + 1]: end } = valueOffsets;
      const entries = value instanceof Map ? value.entries() : Object.entries(value);
      for (const val of entries) {
        set(values, idx, val);
        if (++idx >= end)
          break;
      }
    };
    var _setStructArrayValue = (o, v) => (set, c, _, i) => c && set(c, o, v[i]);
    var _setStructVectorValue = (o, v) => (set, c, _, i) => c && set(c, o, v.get(i));
    var _setStructMapValue = (o, v) => (set, c, f, _) => c && set(c, o, v.get(f.name));
    var _setStructObjectValue = (o, v) => (set, c, f, _) => c && set(c, o, v[f.name]);
    var setStruct = (data, index, value) => {
      const childSetters = data.type.children.map((f) => exports2.instance.getVisitFn(f.type));
      const set = value instanceof Map ? _setStructMapValue(index, value) : value instanceof vector_js_1.Vector ? _setStructVectorValue(index, value) : Array.isArray(value) ? _setStructArrayValue(index, value) : _setStructObjectValue(index, value);
      data.type.children.forEach((f, i) => set(childSetters[i], data.children[i], f, i));
    };
    var setUnion = (data, index, value) => {
      data.type.mode === enum_js_1.UnionMode.Dense ? setDenseUnion(data, index, value) : setSparseUnion(data, index, value);
    };
    var setDenseUnion = (data, index, value) => {
      const childIndex = data.type.typeIdToChildIndex[data.typeIds[index]];
      const child = data.children[childIndex];
      exports2.instance.visit(child, data.valueOffsets[index], value);
    };
    var setSparseUnion = (data, index, value) => {
      const childIndex = data.type.typeIdToChildIndex[data.typeIds[index]];
      const child = data.children[childIndex];
      exports2.instance.visit(child, index, value);
    };
    var setDictionary = (data, index, value) => {
      var _a;
      (_a = data.dictionary) === null || _a === void 0 ? void 0 : _a.set(data.values[index], value);
    };
    var setIntervalValue = (data, index, value) => {
      data.type.unit === enum_js_1.IntervalUnit.DAY_TIME ? (0, exports2.setIntervalDayTime)(data, index, value) : (0, exports2.setIntervalYearMonth)(data, index, value);
    };
    exports2.setIntervalValue = setIntervalValue;
    var setIntervalDayTime = ({ values }, index, value) => {
      values.set(value.subarray(0, 2), 2 * index);
    };
    exports2.setIntervalDayTime = setIntervalDayTime;
    var setIntervalYearMonth = ({ values }, index, value) => {
      values[index] = value[0] * 12 + value[1] % 12;
    };
    exports2.setIntervalYearMonth = setIntervalYearMonth;
    var setDurationSecond = ({ values }, index, value) => {
      values[index] = value;
    };
    exports2.setDurationSecond = setDurationSecond;
    var setDurationMillisecond = ({ values }, index, value) => {
      values[index] = value;
    };
    exports2.setDurationMillisecond = setDurationMillisecond;
    var setDurationMicrosecond = ({ values }, index, value) => {
      values[index] = value;
    };
    exports2.setDurationMicrosecond = setDurationMicrosecond;
    var setDurationNanosecond = ({ values }, index, value) => {
      values[index] = value;
    };
    exports2.setDurationNanosecond = setDurationNanosecond;
    var setDuration = (data, index, value) => {
      switch (data.type.unit) {
        case enum_js_1.TimeUnit.SECOND:
          return (0, exports2.setDurationSecond)(data, index, value);
        case enum_js_1.TimeUnit.MILLISECOND:
          return (0, exports2.setDurationMillisecond)(data, index, value);
        case enum_js_1.TimeUnit.MICROSECOND:
          return (0, exports2.setDurationMicrosecond)(data, index, value);
        case enum_js_1.TimeUnit.NANOSECOND:
          return (0, exports2.setDurationNanosecond)(data, index, value);
      }
    };
    exports2.setDuration = setDuration;
    var setFixedSizeList = (data, index, value) => {
      const { stride } = data;
      const child = data.children[0];
      const set = exports2.instance.getVisitFn(child);
      if (Array.isArray(value)) {
        for (let idx = -1, offset = index * stride; ++idx < stride; ) {
          set(child, offset + idx, value[idx]);
        }
      } else {
        for (let idx = -1, offset = index * stride; ++idx < stride; ) {
          set(child, offset + idx, value.get(idx));
        }
      }
    };
    SetVisitor.prototype.visitBool = wrapSet(setBool);
    SetVisitor.prototype.visitInt = wrapSet(exports2.setInt);
    SetVisitor.prototype.visitInt8 = wrapSet(exports2.setInt);
    SetVisitor.prototype.visitInt16 = wrapSet(exports2.setInt);
    SetVisitor.prototype.visitInt32 = wrapSet(exports2.setInt);
    SetVisitor.prototype.visitInt64 = wrapSet(exports2.setInt);
    SetVisitor.prototype.visitUint8 = wrapSet(exports2.setInt);
    SetVisitor.prototype.visitUint16 = wrapSet(exports2.setInt);
    SetVisitor.prototype.visitUint32 = wrapSet(exports2.setInt);
    SetVisitor.prototype.visitUint64 = wrapSet(exports2.setInt);
    SetVisitor.prototype.visitFloat = wrapSet(exports2.setAnyFloat);
    SetVisitor.prototype.visitFloat16 = wrapSet(exports2.setFloat16);
    SetVisitor.prototype.visitFloat32 = wrapSet(exports2.setFloat);
    SetVisitor.prototype.visitFloat64 = wrapSet(exports2.setFloat);
    SetVisitor.prototype.visitUtf8 = wrapSet(setUtf8);
    SetVisitor.prototype.visitLargeUtf8 = wrapSet(setUtf8);
    SetVisitor.prototype.visitBinary = wrapSet(setBinary);
    SetVisitor.prototype.visitLargeBinary = wrapSet(setBinary);
    SetVisitor.prototype.visitFixedSizeBinary = wrapSet(exports2.setFixedSizeBinary);
    SetVisitor.prototype.visitDate = wrapSet(exports2.setDate);
    SetVisitor.prototype.visitDateDay = wrapSet(exports2.setDateDay);
    SetVisitor.prototype.visitDateMillisecond = wrapSet(exports2.setDateMillisecond);
    SetVisitor.prototype.visitTimestamp = wrapSet(exports2.setTimestamp);
    SetVisitor.prototype.visitTimestampSecond = wrapSet(exports2.setTimestampSecond);
    SetVisitor.prototype.visitTimestampMillisecond = wrapSet(exports2.setTimestampMillisecond);
    SetVisitor.prototype.visitTimestampMicrosecond = wrapSet(exports2.setTimestampMicrosecond);
    SetVisitor.prototype.visitTimestampNanosecond = wrapSet(exports2.setTimestampNanosecond);
    SetVisitor.prototype.visitTime = wrapSet(exports2.setTime);
    SetVisitor.prototype.visitTimeSecond = wrapSet(exports2.setTimeSecond);
    SetVisitor.prototype.visitTimeMillisecond = wrapSet(exports2.setTimeMillisecond);
    SetVisitor.prototype.visitTimeMicrosecond = wrapSet(exports2.setTimeMicrosecond);
    SetVisitor.prototype.visitTimeNanosecond = wrapSet(exports2.setTimeNanosecond);
    SetVisitor.prototype.visitDecimal = wrapSet(exports2.setDecimal);
    SetVisitor.prototype.visitList = wrapSet(setList);
    SetVisitor.prototype.visitStruct = wrapSet(setStruct);
    SetVisitor.prototype.visitUnion = wrapSet(setUnion);
    SetVisitor.prototype.visitDenseUnion = wrapSet(setDenseUnion);
    SetVisitor.prototype.visitSparseUnion = wrapSet(setSparseUnion);
    SetVisitor.prototype.visitDictionary = wrapSet(setDictionary);
    SetVisitor.prototype.visitInterval = wrapSet(exports2.setIntervalValue);
    SetVisitor.prototype.visitIntervalDayTime = wrapSet(exports2.setIntervalDayTime);
    SetVisitor.prototype.visitIntervalYearMonth = wrapSet(exports2.setIntervalYearMonth);
    SetVisitor.prototype.visitDuration = wrapSet(exports2.setDuration);
    SetVisitor.prototype.visitDurationSecond = wrapSet(exports2.setDurationSecond);
    SetVisitor.prototype.visitDurationMillisecond = wrapSet(exports2.setDurationMillisecond);
    SetVisitor.prototype.visitDurationMicrosecond = wrapSet(exports2.setDurationMicrosecond);
    SetVisitor.prototype.visitDurationNanosecond = wrapSet(exports2.setDurationNanosecond);
    SetVisitor.prototype.visitFixedSizeList = wrapSet(setFixedSizeList);
    SetVisitor.prototype.visitMap = wrapSet(setMap);
    exports2.instance = new SetVisitor();
  }
});

// node_modules/apache-arrow/row/struct.js
var require_struct2 = __commonJS({
  "node_modules/apache-arrow/row/struct.js"(exports2) {
    "use strict";
    Object.defineProperty(exports2, "__esModule", { value: true });
    exports2.StructRow = void 0;
    var pretty_js_1 = require_pretty();
    var get_js_1 = require_get();
    var set_js_1 = require_set();
    var kParent = /* @__PURE__ */ Symbol.for("parent");
    var kRowIndex = /* @__PURE__ */ Symbol.for("rowIndex");
    var StructRow = class {
      constructor(parent, rowIndex) {
        this[kParent] = parent;
        this[kRowIndex] = rowIndex;
        return new Proxy(this, new StructRowProxyHandler());
      }
      toArray() {
        return Object.values(this.toJSON());
      }
      toJSON() {
        const i = this[kRowIndex];
        const parent = this[kParent];
        const keys = parent.type.children;
        const json = {};
        for (let j = -1, n = keys.length; ++j < n; ) {
          json[keys[j].name] = get_js_1.instance.visit(parent.children[j], i);
        }
        return json;
      }
      toString() {
        return `{${[...this].map(([key, val]) => `${(0, pretty_js_1.valueToString)(key)}: ${(0, pretty_js_1.valueToString)(val)}`).join(", ")}}`;
      }
      [/* @__PURE__ */ Symbol.for("nodejs.util.inspect.custom")]() {
        return this.toString();
      }
      [Symbol.iterator]() {
        return new StructRowIterator(this[kParent], this[kRowIndex]);
      }
    };
    exports2.StructRow = StructRow;
    var StructRowIterator = class {
      constructor(data, rowIndex) {
        this.childIndex = 0;
        this.children = data.children;
        this.rowIndex = rowIndex;
        this.childFields = data.type.children;
        this.numChildren = this.childFields.length;
      }
      [Symbol.iterator]() {
        return this;
      }
      next() {
        const i = this.childIndex;
        if (i < this.numChildren) {
          this.childIndex = i + 1;
          return {
            done: false,
            value: [
              this.childFields[i].name,
              get_js_1.instance.visit(this.children[i], this.rowIndex)
            ]
          };
        }
        return { done: true, value: null };
      }
    };
    Object.defineProperties(StructRow.prototype, {
      [Symbol.toStringTag]: { enumerable: false, configurable: false, value: "Row" },
      [kParent]: { writable: true, enumerable: false, configurable: false, value: null },
      [kRowIndex]: { writable: true, enumerable: false, configurable: false, value: -1 }
    });
    var StructRowProxyHandler = class {
      isExtensible() {
        return false;
      }
      deleteProperty() {
        return false;
      }
      preventExtensions() {
        return true;
      }
      ownKeys(row) {
        return row[kParent].type.children.map((f) => f.name);
      }
      has(row, key) {
        return row[kParent].type.children.findIndex((f) => f.name === key) !== -1;
      }
      getOwnPropertyDescriptor(row, key) {
        if (row[kParent].type.children.findIndex((f) => f.name === key) !== -1) {
          return { writable: true, enumerable: true, configurable: true };
        }
        return;
      }
      get(row, key) {
        if (Reflect.has(row, key)) {
          return row[key];
        }
        const idx = row[kParent].type.children.findIndex((f) => f.name === key);
        if (idx !== -1) {
          const val = get_js_1.instance.visit(row[kParent].children[idx], row[kRowIndex]);
          Reflect.set(row, key, val);
          return val;
        }
      }
      set(row, key, val) {
        const idx = row[kParent].type.children.findIndex((f) => f.name === key);
        if (idx !== -1) {
          set_js_1.instance.visit(row[kParent].children[idx], row[kRowIndex], val);
          return Reflect.set(row, key, val);
        } else if (Reflect.has(row, key) || typeof key === "symbol") {
          return Reflect.set(row, key, val);
        }
        return false;
      }
    };
  }
});

// node_modules/apache-arrow/visitor/get.js
var require_get = __commonJS({
  "node_modules/apache-arrow/visitor/get.js"(exports2) {
    "use strict";
    Object.defineProperty(exports2, "__esModule", { value: true });
    exports2.instance = exports2.GetVisitor = void 0;
    var bn_js_1 = require_bn();
    var vector_js_1 = require_vector2();
    var visitor_js_1 = require_visitor();
    var map_js_1 = require_map2();
    var struct_js_1 = require_struct2();
    var bigint_js_1 = require_bigint();
    var utf8_js_1 = require_utf8();
    var math_js_1 = require_math();
    var enum_js_1 = require_enum();
    var GetVisitor = class extends visitor_js_1.Visitor {
    };
    exports2.GetVisitor = GetVisitor;
    function wrapGet(fn) {
      return (data, _1) => data.getValid(_1) ? fn(data, _1) : null;
    }
    var epochDaysToMs = (data, index) => 864e5 * data[index];
    var getNull = (_data, _index) => null;
    var getVariableWidthBytes = (values, valueOffsets, index) => {
      if (index + 1 >= valueOffsets.length) {
        return null;
      }
      const x = (0, bigint_js_1.bigIntToNumber)(valueOffsets[index]);
      const y = (0, bigint_js_1.bigIntToNumber)(valueOffsets[index + 1]);
      return values.subarray(x, y);
    };
    var getBool = ({ offset, values }, index) => {
      const idx = offset + index;
      const byte = values[idx >> 3];
      return (byte & 1 << idx % 8) !== 0;
    };
    var getDateDay = ({ values }, index) => epochDaysToMs(values, index);
    var getDateMillisecond = ({ values }, index) => (0, bigint_js_1.bigIntToNumber)(values[index]);
    var getNumeric = ({ stride, values }, index) => values[stride * index];
    var getFloat16 = ({ stride, values }, index) => (0, math_js_1.uint16ToFloat64)(values[stride * index]);
    var getBigInts = ({ values }, index) => values[index];
    var getFixedSizeBinary = ({ stride, values }, index) => values.subarray(stride * index, stride * (index + 1));
    var getBinary = ({ values, valueOffsets }, index) => getVariableWidthBytes(values, valueOffsets, index);
    var getUtf8 = ({ values, valueOffsets }, index) => {
      const bytes = getVariableWidthBytes(values, valueOffsets, index);
      return bytes !== null ? (0, utf8_js_1.decodeUtf8)(bytes) : null;
    };
    var getInt = ({ values }, index) => values[index];
    var getFloat = ({ type, values }, index) => type.precision !== enum_js_1.Precision.HALF ? values[index] : (0, math_js_1.uint16ToFloat64)(values[index]);
    var getDate = (data, index) => data.type.unit === enum_js_1.DateUnit.DAY ? getDateDay(data, index) : getDateMillisecond(data, index);
    var getTimestampSecond = ({ values }, index) => 1e3 * (0, bigint_js_1.bigIntToNumber)(values[index]);
    var getTimestampMillisecond = ({ values }, index) => (0, bigint_js_1.bigIntToNumber)(values[index]);
    var getTimestampMicrosecond = ({ values }, index) => (0, bigint_js_1.divideBigInts)(values[index], BigInt(1e3));
    var getTimestampNanosecond = ({ values }, index) => (0, bigint_js_1.divideBigInts)(values[index], BigInt(1e6));
    var getTimestamp = (data, index) => {
      switch (data.type.unit) {
        case enum_js_1.TimeUnit.SECOND:
          return getTimestampSecond(data, index);
        case enum_js_1.TimeUnit.MILLISECOND:
          return getTimestampMillisecond(data, index);
        case enum_js_1.TimeUnit.MICROSECOND:
          return getTimestampMicrosecond(data, index);
        case enum_js_1.TimeUnit.NANOSECOND:
          return getTimestampNanosecond(data, index);
      }
    };
    var getTimeSecond = ({ values }, index) => values[index];
    var getTimeMillisecond = ({ values }, index) => values[index];
    var getTimeMicrosecond = ({ values }, index) => values[index];
    var getTimeNanosecond = ({ values }, index) => values[index];
    var getTime = (data, index) => {
      switch (data.type.unit) {
        case enum_js_1.TimeUnit.SECOND:
          return getTimeSecond(data, index);
        case enum_js_1.TimeUnit.MILLISECOND:
          return getTimeMillisecond(data, index);
        case enum_js_1.TimeUnit.MICROSECOND:
          return getTimeMicrosecond(data, index);
        case enum_js_1.TimeUnit.NANOSECOND:
          return getTimeNanosecond(data, index);
      }
    };
    var getDecimal = ({ values, stride }, index) => bn_js_1.BN.decimal(values.subarray(stride * index, stride * (index + 1)));
    var getList = (data, index) => {
      const { valueOffsets, stride, children } = data;
      const { [index * stride]: begin, [index * stride + 1]: end } = valueOffsets;
      const child = children[0];
      const slice = child.slice(begin, end - begin);
      return new vector_js_1.Vector([slice]);
    };
    var getMap = (data, index) => {
      const { valueOffsets, children } = data;
      const { [index]: begin, [index + 1]: end } = valueOffsets;
      const child = children[0];
      return new map_js_1.MapRow(child.slice(begin, end - begin));
    };
    var getStruct = (data, index) => {
      return new struct_js_1.StructRow(data, index);
    };
    var getUnion = (data, index) => {
      return data.type.mode === enum_js_1.UnionMode.Dense ? getDenseUnion(data, index) : getSparseUnion(data, index);
    };
    var getDenseUnion = (data, index) => {
      const childIndex = data.type.typeIdToChildIndex[data.typeIds[index]];
      const child = data.children[childIndex];
      return exports2.instance.visit(child, data.valueOffsets[index]);
    };
    var getSparseUnion = (data, index) => {
      const childIndex = data.type.typeIdToChildIndex[data.typeIds[index]];
      const child = data.children[childIndex];
      return exports2.instance.visit(child, index);
    };
    var getDictionary = (data, index) => {
      var _a;
      return (_a = data.dictionary) === null || _a === void 0 ? void 0 : _a.get(data.values[index]);
    };
    var getInterval = (data, index) => data.type.unit === enum_js_1.IntervalUnit.DAY_TIME ? getIntervalDayTime(data, index) : getIntervalYearMonth(data, index);
    var getIntervalDayTime = ({ values }, index) => values.subarray(2 * index, 2 * (index + 1));
    var getIntervalYearMonth = ({ values }, index) => {
      const interval = values[index];
      const int32s = new Int32Array(2);
      int32s[0] = Math.trunc(interval / 12);
      int32s[1] = Math.trunc(interval % 12);
      return int32s;
    };
    var getDurationSecond = ({ values }, index) => values[index];
    var getDurationMillisecond = ({ values }, index) => values[index];
    var getDurationMicrosecond = ({ values }, index) => values[index];
    var getDurationNanosecond = ({ values }, index) => values[index];
    var getDuration = (data, index) => {
      switch (data.type.unit) {
        case enum_js_1.TimeUnit.SECOND:
          return getDurationSecond(data, index);
        case enum_js_1.TimeUnit.MILLISECOND:
          return getDurationMillisecond(data, index);
        case enum_js_1.TimeUnit.MICROSECOND:
          return getDurationMicrosecond(data, index);
        case enum_js_1.TimeUnit.NANOSECOND:
          return getDurationNanosecond(data, index);
      }
    };
    var getFixedSizeList = (data, index) => {
      const { stride, children } = data;
      const child = children[0];
      const slice = child.slice(index * stride, stride);
      return new vector_js_1.Vector([slice]);
    };
    GetVisitor.prototype.visitNull = wrapGet(getNull);
    GetVisitor.prototype.visitBool = wrapGet(getBool);
    GetVisitor.prototype.visitInt = wrapGet(getInt);
    GetVisitor.prototype.visitInt8 = wrapGet(getNumeric);
    GetVisitor.prototype.visitInt16 = wrapGet(getNumeric);
    GetVisitor.prototype.visitInt32 = wrapGet(getNumeric);
    GetVisitor.prototype.visitInt64 = wrapGet(getBigInts);
    GetVisitor.prototype.visitUint8 = wrapGet(getNumeric);
    GetVisitor.prototype.visitUint16 = wrapGet(getNumeric);
    GetVisitor.prototype.visitUint32 = wrapGet(getNumeric);
    GetVisitor.prototype.visitUint64 = wrapGet(getBigInts);
    GetVisitor.prototype.visitFloat = wrapGet(getFloat);
    GetVisitor.prototype.visitFloat16 = wrapGet(getFloat16);
    GetVisitor.prototype.visitFloat32 = wrapGet(getNumeric);
    GetVisitor.prototype.visitFloat64 = wrapGet(getNumeric);
    GetVisitor.prototype.visitUtf8 = wrapGet(getUtf8);
    GetVisitor.prototype.visitLargeUtf8 = wrapGet(getUtf8);
    GetVisitor.prototype.visitBinary = wrapGet(getBinary);
    GetVisitor.prototype.visitLargeBinary = wrapGet(getBinary);
    GetVisitor.prototype.visitFixedSizeBinary = wrapGet(getFixedSizeBinary);
    GetVisitor.prototype.visitDate = wrapGet(getDate);
    GetVisitor.prototype.visitDateDay = wrapGet(getDateDay);
    GetVisitor.prototype.visitDateMillisecond = wrapGet(getDateMillisecond);
    GetVisitor.prototype.visitTimestamp = wrapGet(getTimestamp);
    GetVisitor.prototype.visitTimestampSecond = wrapGet(getTimestampSecond);
    GetVisitor.prototype.visitTimestampMillisecond = wrapGet(getTimestampMillisecond);
    GetVisitor.prototype.visitTimestampMicrosecond = wrapGet(getTimestampMicrosecond);
    GetVisitor.prototype.visitTimestampNanosecond = wrapGet(getTimestampNanosecond);
    GetVisitor.prototype.visitTime = wrapGet(getTime);
    GetVisitor.prototype.visitTimeSecond = wrapGet(getTimeSecond);
    GetVisitor.prototype.visitTimeMillisecond = wrapGet(getTimeMillisecond);
    GetVisitor.prototype.visitTimeMicrosecond = wrapGet(getTimeMicrosecond);
    GetVisitor.prototype.visitTimeNanosecond = wrapGet(getTimeNanosecond);
    GetVisitor.prototype.visitDecimal = wrapGet(getDecimal);
    GetVisitor.prototype.visitList = wrapGet(getList);
    GetVisitor.prototype.visitStruct = wrapGet(getStruct);
    GetVisitor.prototype.visitUnion = wrapGet(getUnion);
    GetVisitor.prototype.visitDenseUnion = wrapGet(getDenseUnion);
    GetVisitor.prototype.visitSparseUnion = wrapGet(getSparseUnion);
    GetVisitor.prototype.visitDictionary = wrapGet(getDictionary);
    GetVisitor.prototype.visitInterval = wrapGet(getInterval);
    GetVisitor.prototype.visitIntervalDayTime = wrapGet(getIntervalDayTime);
    GetVisitor.prototype.visitIntervalYearMonth = wrapGet(getIntervalYearMonth);
    GetVisitor.prototype.visitDuration = wrapGet(getDuration);
    GetVisitor.prototype.visitDurationSecond = wrapGet(getDurationSecond);
    GetVisitor.prototype.visitDurationMillisecond = wrapGet(getDurationMillisecond);
    GetVisitor.prototype.visitDurationMicrosecond = wrapGet(getDurationMicrosecond);
    GetVisitor.prototype.visitDurationNanosecond = wrapGet(getDurationNanosecond);
    GetVisitor.prototype.visitFixedSizeList = wrapGet(getFixedSizeList);
    GetVisitor.prototype.visitMap = wrapGet(getMap);
    exports2.instance = new GetVisitor();
  }
});

// node_modules/apache-arrow/row/map.js
var require_map2 = __commonJS({
  "node_modules/apache-arrow/row/map.js"(exports2) {
    "use strict";
    Object.defineProperty(exports2, "__esModule", { value: true });
    exports2.MapRow = exports2._kKeysAsStrings = exports2.kKeysAsStrings = exports2.kVals = exports2.kKeys = void 0;
    var vector_js_1 = require_vector2();
    var pretty_js_1 = require_pretty();
    var get_js_1 = require_get();
    var set_js_1 = require_set();
    exports2.kKeys = /* @__PURE__ */ Symbol.for("keys");
    exports2.kVals = /* @__PURE__ */ Symbol.for("vals");
    exports2.kKeysAsStrings = /* @__PURE__ */ Symbol.for("kKeysAsStrings");
    exports2._kKeysAsStrings = /* @__PURE__ */ Symbol.for("_kKeysAsStrings");
    var MapRow = class {
      constructor(slice) {
        this[exports2.kKeys] = new vector_js_1.Vector([slice.children[0]]).memoize();
        this[exports2.kVals] = slice.children[1];
        return new Proxy(this, new MapRowProxyHandler());
      }
      /** @ignore */
      get [exports2.kKeysAsStrings]() {
        return this[exports2._kKeysAsStrings] || (this[exports2._kKeysAsStrings] = Array.from(this[exports2.kKeys].toArray(), String));
      }
      [Symbol.iterator]() {
        return new MapRowIterator(this[exports2.kKeys], this[exports2.kVals]);
      }
      get size() {
        return this[exports2.kKeys].length;
      }
      toArray() {
        return Object.values(this.toJSON());
      }
      toJSON() {
        const keys = this[exports2.kKeys];
        const vals = this[exports2.kVals];
        const json = {};
        for (let i = -1, n = keys.length; ++i < n; ) {
          json[keys.get(i)] = get_js_1.instance.visit(vals, i);
        }
        return json;
      }
      toString() {
        return `{${[...this].map(([key, val]) => `${(0, pretty_js_1.valueToString)(key)}: ${(0, pretty_js_1.valueToString)(val)}`).join(", ")}}`;
      }
      [/* @__PURE__ */ Symbol.for("nodejs.util.inspect.custom")]() {
        return this.toString();
      }
    };
    exports2.MapRow = MapRow;
    var MapRowIterator = class {
      constructor(keys, vals) {
        this.keys = keys;
        this.vals = vals;
        this.keyIndex = 0;
        this.numKeys = keys.length;
      }
      [Symbol.iterator]() {
        return this;
      }
      next() {
        const i = this.keyIndex;
        if (i === this.numKeys) {
          return { done: true, value: null };
        }
        this.keyIndex++;
        return {
          done: false,
          value: [
            this.keys.get(i),
            get_js_1.instance.visit(this.vals, i)
          ]
        };
      }
    };
    var MapRowProxyHandler = class {
      isExtensible() {
        return false;
      }
      deleteProperty() {
        return false;
      }
      preventExtensions() {
        return true;
      }
      ownKeys(row) {
        return row[exports2.kKeysAsStrings];
      }
      has(row, key) {
        return row[exports2.kKeysAsStrings].includes(key);
      }
      getOwnPropertyDescriptor(row, key) {
        const idx = row[exports2.kKeysAsStrings].indexOf(key);
        if (idx !== -1) {
          return { writable: true, enumerable: true, configurable: true };
        }
        return;
      }
      get(row, key) {
        if (Reflect.has(row, key)) {
          return row[key];
        }
        const idx = row[exports2.kKeysAsStrings].indexOf(key);
        if (idx !== -1) {
          const val = get_js_1.instance.visit(Reflect.get(row, exports2.kVals), idx);
          Reflect.set(row, key, val);
          return val;
        }
      }
      set(row, key, val) {
        const idx = row[exports2.kKeysAsStrings].indexOf(key);
        if (idx !== -1) {
          set_js_1.instance.visit(Reflect.get(row, exports2.kVals), idx, val);
          return Reflect.set(row, key, val);
        } else if (Reflect.has(row, key)) {
          return Reflect.set(row, key, val);
        }
        return false;
      }
    };
    Object.defineProperties(MapRow.prototype, {
      [Symbol.toStringTag]: { enumerable: false, configurable: false, value: "Row" },
      [exports2.kKeys]: { writable: true, enumerable: false, configurable: false, value: null },
      [exports2.kVals]: { writable: true, enumerable: false, configurable: false, value: null },
      [exports2._kKeysAsStrings]: { writable: true, enumerable: false, configurable: false, value: null }
    });
  }
});

// node_modules/apache-arrow/util/vector.js
var require_vector = __commonJS({
  "node_modules/apache-arrow/util/vector.js"(exports2) {
    "use strict";
    Object.defineProperty(exports2, "__esModule", { value: true });
    exports2.createElementComparator = exports2.wrapIndex = exports2.clampRange = void 0;
    var vector_js_1 = require_vector2();
    var map_js_1 = require_map2();
    var struct_js_1 = require_struct2();
    var buffer_js_1 = require_buffer();
    var tmp;
    function clampRange(source, begin, end, then) {
      const { length: len = 0 } = source;
      let lhs = typeof begin !== "number" ? 0 : begin;
      let rhs = typeof end !== "number" ? len : end;
      lhs < 0 && (lhs = (lhs % len + len) % len);
      rhs < 0 && (rhs = (rhs % len + len) % len);
      rhs < lhs && (tmp = lhs, lhs = rhs, rhs = tmp);
      rhs > len && (rhs = len);
      return then ? then(source, lhs, rhs) : [lhs, rhs];
    }
    exports2.clampRange = clampRange;
    var wrapIndex = (index, len) => index < 0 ? len + index : index;
    exports2.wrapIndex = wrapIndex;
    var isNaNFast = (value) => value !== value;
    function createElementComparator(search) {
      const typeofSearch = typeof search;
      if (typeofSearch !== "object" || search === null) {
        if (isNaNFast(search)) {
          return isNaNFast;
        }
        return (value) => value === search;
      }
      if (search instanceof Date) {
        const valueOfSearch = search.valueOf();
        return (value) => value instanceof Date ? value.valueOf() === valueOfSearch : false;
      }
      if (ArrayBuffer.isView(search)) {
        return (value) => value ? (0, buffer_js_1.compareArrayLike)(search, value) : false;
      }
      if (search instanceof Map) {
        return createMapComparator(search);
      }
      if (Array.isArray(search)) {
        return createArrayLikeComparator(search);
      }
      if (search instanceof vector_js_1.Vector) {
        return createVectorComparator(search);
      }
      return createObjectComparator(search, true);
    }
    exports2.createElementComparator = createElementComparator;
    function createArrayLikeComparator(lhs) {
      const comparators = [];
      for (let i = -1, n = lhs.length; ++i < n; ) {
        comparators[i] = createElementComparator(lhs[i]);
      }
      return createSubElementsComparator(comparators);
    }
    function createMapComparator(lhs) {
      let i = -1;
      const comparators = [];
      for (const v of lhs.values())
        comparators[++i] = createElementComparator(v);
      return createSubElementsComparator(comparators);
    }
    function createVectorComparator(lhs) {
      const comparators = [];
      for (let i = -1, n = lhs.length; ++i < n; ) {
        comparators[i] = createElementComparator(lhs.get(i));
      }
      return createSubElementsComparator(comparators);
    }
    function createObjectComparator(lhs, allowEmpty = false) {
      const keys = Object.keys(lhs);
      if (!allowEmpty && keys.length === 0) {
        return () => false;
      }
      const comparators = [];
      for (let i = -1, n = keys.length; ++i < n; ) {
        comparators[i] = createElementComparator(lhs[keys[i]]);
      }
      return createSubElementsComparator(comparators, keys);
    }
    function createSubElementsComparator(comparators, keys) {
      return (rhs) => {
        if (!rhs || typeof rhs !== "object") {
          return false;
        }
        switch (rhs.constructor) {
          case Array:
            return compareArray(comparators, rhs);
          case Map:
            return compareObject(comparators, rhs, rhs.keys());
          case map_js_1.MapRow:
          case struct_js_1.StructRow:
          case Object:
          case void 0:
            return compareObject(comparators, rhs, keys || Object.keys(rhs));
        }
        return rhs instanceof vector_js_1.Vector ? compareVector(comparators, rhs) : false;
      };
    }
    function compareArray(comparators, arr) {
      const n = comparators.length;
      if (arr.length !== n) {
        return false;
      }
      for (let i = -1; ++i < n; ) {
        if (!comparators[i](arr[i])) {
          return false;
        }
      }
      return true;
    }
    function compareVector(comparators, vec) {
      const n = comparators.length;
      if (vec.length !== n) {
        return false;
      }
      for (let i = -1; ++i < n; ) {
        if (!comparators[i](vec.get(i))) {
          return false;
        }
      }
      return true;
    }
    function compareObject(comparators, obj, keys) {
      const lKeyItr = keys[Symbol.iterator]();
      const rKeyItr = obj instanceof Map ? obj.keys() : Object.keys(obj)[Symbol.iterator]();
      const rValItr = obj instanceof Map ? obj.values() : Object.values(obj)[Symbol.iterator]();
      let i = 0;
      const n = comparators.length;
      let rVal = rValItr.next();
      let lKey = lKeyItr.next();
      let rKey = rKeyItr.next();
      for (; i < n && !lKey.done && !rKey.done && !rVal.done; ++i, lKey = lKeyItr.next(), rKey = rKeyItr.next(), rVal = rValItr.next()) {
        if (lKey.value !== rKey.value || !comparators[i](rVal.value)) {
          break;
        }
      }
      if (i === n && lKey.done && rKey.done && rVal.done) {
        return true;
      }
      lKeyItr.return && lKeyItr.return();
      rKeyItr.return && rKeyItr.return();
      rValItr.return && rValItr.return();
      return false;
    }
  }
});

// node_modules/apache-arrow/util/bit.js
var require_bit = __commonJS({
  "node_modules/apache-arrow/util/bit.js"(exports2) {
    "use strict";
    Object.defineProperty(exports2, "__esModule", { value: true });
    exports2.popcnt_uint32 = exports2.popcnt_array = exports2.popcnt_bit_range = exports2.BitIterator = exports2.packBools = exports2.truncateBitmap = exports2.setBool = exports2.getBit = exports2.getBool = void 0;
    function getBool(_data, _index, byte, bit) {
      return (byte & 1 << bit) !== 0;
    }
    exports2.getBool = getBool;
    function getBit(_data, _index, byte, bit) {
      return (byte & 1 << bit) >> bit;
    }
    exports2.getBit = getBit;
    function setBool(bytes, index, value) {
      return value ? !!(bytes[index >> 3] |= 1 << index % 8) || true : !(bytes[index >> 3] &= ~(1 << index % 8)) && false;
    }
    exports2.setBool = setBool;
    function truncateBitmap(offset, length, bitmap) {
      const alignedSize = bitmap.byteLength + 7 & ~7;
      if (offset > 0 || bitmap.byteLength < alignedSize) {
        const bytes = new Uint8Array(alignedSize);
        bytes.set(offset % 8 === 0 ? bitmap.subarray(offset >> 3) : (
          // Otherwise iterate each bit from the offset and return a new one
          packBools(new BitIterator(bitmap, offset, length, null, getBool)).subarray(0, alignedSize)
        ));
        return bytes;
      }
      return bitmap;
    }
    exports2.truncateBitmap = truncateBitmap;
    function packBools(values) {
      const xs = [];
      let i = 0, bit = 0, byte = 0;
      for (const value of values) {
        value && (byte |= 1 << bit);
        if (++bit === 8) {
          xs[i++] = byte;
          byte = bit = 0;
        }
      }
      if (i === 0 || bit > 0) {
        xs[i++] = byte;
      }
      const b = new Uint8Array(xs.length + 7 & ~7);
      b.set(xs);
      return b;
    }
    exports2.packBools = packBools;
    var BitIterator = class {
      constructor(bytes, begin, length, context, get) {
        this.bytes = bytes;
        this.length = length;
        this.context = context;
        this.get = get;
        this.bit = begin % 8;
        this.byteIndex = begin >> 3;
        this.byte = bytes[this.byteIndex++];
        this.index = 0;
      }
      next() {
        if (this.index < this.length) {
          if (this.bit === 8) {
            this.bit = 0;
            this.byte = this.bytes[this.byteIndex++];
          }
          return {
            value: this.get(this.context, this.index++, this.byte, this.bit++)
          };
        }
        return { done: true, value: null };
      }
      [Symbol.iterator]() {
        return this;
      }
    };
    exports2.BitIterator = BitIterator;
    function popcnt_bit_range(data, lhs, rhs) {
      if (rhs - lhs <= 0) {
        return 0;
      }
      if (rhs - lhs < 8) {
        let sum = 0;
        for (const bit of new BitIterator(data, lhs, rhs - lhs, data, getBit)) {
          sum += bit;
        }
        return sum;
      }
      const rhsInside = rhs >> 3 << 3;
      const lhsInside = lhs + (lhs % 8 === 0 ? 0 : 8 - lhs % 8);
      return (
        // Get the popcnt of bits between the left hand side, and the next highest multiple of 8
        popcnt_bit_range(data, lhs, lhsInside) + // Get the popcnt of bits between the right hand side, and the next lowest multiple of 8
        popcnt_bit_range(data, rhsInside, rhs) + // Get the popcnt of all bits between the left and right hand sides' multiples of 8
        popcnt_array(data, lhsInside >> 3, rhsInside - lhsInside >> 3)
      );
    }
    exports2.popcnt_bit_range = popcnt_bit_range;
    function popcnt_array(arr, byteOffset, byteLength) {
      let cnt = 0, pos = Math.trunc(byteOffset);
      const view = new DataView(arr.buffer, arr.byteOffset, arr.byteLength);
      const len = byteLength === void 0 ? arr.byteLength : pos + byteLength;
      while (len - pos >= 4) {
        cnt += popcnt_uint32(view.getUint32(pos));
        pos += 4;
      }
      while (len - pos >= 2) {
        cnt += popcnt_uint32(view.getUint16(pos));
        pos += 2;
      }
      while (len - pos >= 1) {
        cnt += popcnt_uint32(view.getUint8(pos));
        pos += 1;
      }
      return cnt;
    }
    exports2.popcnt_array = popcnt_array;
    function popcnt_uint32(uint32) {
      let i = Math.trunc(uint32);
      i = i - (i >>> 1 & 1431655765);
      i = (i & 858993459) + (i >>> 2 & 858993459);
      return (i + (i >>> 4) & 252645135) * 16843009 >>> 24;
    }
    exports2.popcnt_uint32 = popcnt_uint32;
  }
});

// node_modules/apache-arrow/data.js
var require_data = __commonJS({
  "node_modules/apache-arrow/data.js"(exports2) {
    "use strict";
    Object.defineProperty(exports2, "__esModule", { value: true });
    exports2.makeData = exports2.Data = exports2.kUnknownNullCount = void 0;
    var vector_js_1 = require_vector2();
    var enum_js_1 = require_enum();
    var type_js_1 = require_type2();
    var bit_js_1 = require_bit();
    exports2.kUnknownNullCount = -1;
    var Data = class _Data {
      get typeId() {
        return this.type.typeId;
      }
      get ArrayType() {
        return this.type.ArrayType;
      }
      get buffers() {
        return [this.valueOffsets, this.values, this.nullBitmap, this.typeIds];
      }
      get nullable() {
        if (this._nullCount !== 0) {
          const { type } = this;
          if (type_js_1.DataType.isSparseUnion(type)) {
            return this.children.some((child) => child.nullable);
          } else if (type_js_1.DataType.isDenseUnion(type)) {
            return this.children.some((child) => child.nullable);
          }
          return this.nullBitmap && this.nullBitmap.byteLength > 0;
        }
        return true;
      }
      get byteLength() {
        let byteLength = 0;
        const { valueOffsets, values, nullBitmap, typeIds } = this;
        valueOffsets && (byteLength += valueOffsets.byteLength);
        values && (byteLength += values.byteLength);
        nullBitmap && (byteLength += nullBitmap.byteLength);
        typeIds && (byteLength += typeIds.byteLength);
        return this.children.reduce((byteLength2, child) => byteLength2 + child.byteLength, byteLength);
      }
      get nullCount() {
        if (type_js_1.DataType.isUnion(this.type)) {
          return this.children.reduce((nullCount2, child) => nullCount2 + child.nullCount, 0);
        }
        let nullCount = this._nullCount;
        let nullBitmap;
        if (nullCount <= exports2.kUnknownNullCount && (nullBitmap = this.nullBitmap)) {
          this._nullCount = nullCount = nullBitmap.length === 0 ? (
            // no null bitmap, so all values are valid
            0
          ) : this.length - (0, bit_js_1.popcnt_bit_range)(nullBitmap, this.offset, this.offset + this.length);
        }
        return nullCount;
      }
      constructor(type, offset, length, nullCount, buffers, children = [], dictionary) {
        this.type = type;
        this.children = children;
        this.dictionary = dictionary;
        this.offset = Math.floor(Math.max(offset || 0, 0));
        this.length = Math.floor(Math.max(length || 0, 0));
        this._nullCount = Math.floor(Math.max(nullCount || 0, -1));
        let buffer;
        if (buffers instanceof _Data) {
          this.stride = buffers.stride;
          this.values = buffers.values;
          this.typeIds = buffers.typeIds;
          this.nullBitmap = buffers.nullBitmap;
          this.valueOffsets = buffers.valueOffsets;
        } else {
          this.stride = (0, type_js_1.strideForType)(type);
          if (buffers) {
            (buffer = buffers[0]) && (this.valueOffsets = buffer);
            (buffer = buffers[1]) && (this.values = buffer);
            (buffer = buffers[2]) && (this.nullBitmap = buffer);
            (buffer = buffers[3]) && (this.typeIds = buffer);
          }
        }
      }
      getValid(index) {
        const { type } = this;
        if (type_js_1.DataType.isUnion(type)) {
          const union = type;
          const child = this.children[union.typeIdToChildIndex[this.typeIds[index]]];
          const indexInChild = union.mode === enum_js_1.UnionMode.Dense ? this.valueOffsets[index] : index;
          return child.getValid(indexInChild);
        }
        if (this.nullable && this.nullCount > 0) {
          const pos = this.offset + index;
          const val = this.nullBitmap[pos >> 3];
          return (val & 1 << pos % 8) !== 0;
        }
        return true;
      }
      setValid(index, value) {
        let prev;
        const { type } = this;
        if (type_js_1.DataType.isUnion(type)) {
          const union = type;
          const child = this.children[union.typeIdToChildIndex[this.typeIds[index]]];
          const indexInChild = union.mode === enum_js_1.UnionMode.Dense ? this.valueOffsets[index] : index;
          prev = child.getValid(indexInChild);
          child.setValid(indexInChild, value);
        } else {
          let { nullBitmap } = this;
          const { offset, length } = this;
          const idx = offset + index;
          const mask = 1 << idx % 8;
          const byteOffset = idx >> 3;
          if (!nullBitmap || nullBitmap.byteLength <= byteOffset) {
            nullBitmap = new Uint8Array((offset + length + 63 & ~63) >> 3).fill(255);
            if (this.nullCount > 0) {
              nullBitmap.set((0, bit_js_1.truncateBitmap)(offset, length, this.nullBitmap), 0);
              Object.assign(this, { nullBitmap });
            } else {
              Object.assign(this, { nullBitmap, _nullCount: 0 });
            }
          }
          const byte = nullBitmap[byteOffset];
          prev = (byte & mask) !== 0;
          nullBitmap[byteOffset] = value ? byte | mask : byte & ~mask;
        }
        if (prev !== !!value) {
          this._nullCount = this.nullCount + (value ? -1 : 1);
        }
        return value;
      }
      clone(type = this.type, offset = this.offset, length = this.length, nullCount = this._nullCount, buffers = this, children = this.children) {
        return new _Data(type, offset, length, nullCount, buffers, children, this.dictionary);
      }
      slice(offset, length) {
        const { stride, typeId, children } = this;
        const nullCount = +(this._nullCount === 0) - 1;
        const childStride = typeId === 16 ? stride : 1;
        const buffers = this._sliceBuffers(offset, length, stride, typeId);
        return this.clone(
          this.type,
          this.offset + offset,
          length,
          nullCount,
          buffers,
          // Don't slice children if we have value offsets (the variable-width types)
          children.length === 0 || this.valueOffsets ? children : this._sliceChildren(children, childStride * offset, childStride * length)
        );
      }
      _changeLengthAndBackfillNullBitmap(newLength) {
        if (this.typeId === enum_js_1.Type.Null) {
          return this.clone(this.type, 0, newLength, 0);
        }
        const { length, nullCount } = this;
        const bitmap = new Uint8Array((newLength + 63 & ~63) >> 3).fill(255, 0, length >> 3);
        bitmap[length >> 3] = (1 << length - (length & ~7)) - 1;
        if (nullCount > 0) {
          bitmap.set((0, bit_js_1.truncateBitmap)(this.offset, length, this.nullBitmap), 0);
        }
        const buffers = this.buffers;
        buffers[enum_js_1.BufferType.VALIDITY] = bitmap;
        return this.clone(this.type, 0, newLength, nullCount + (newLength - length), buffers);
      }
      _sliceBuffers(offset, length, stride, typeId) {
        let arr;
        const { buffers } = this;
        (arr = buffers[enum_js_1.BufferType.TYPE]) && (buffers[enum_js_1.BufferType.TYPE] = arr.subarray(offset, offset + length));
        (arr = buffers[enum_js_1.BufferType.OFFSET]) && (buffers[enum_js_1.BufferType.OFFSET] = arr.subarray(offset, offset + length + 1)) || // Otherwise if no offsets, slice the data buffer. Don't slice the data vector for Booleans, since the offset goes by bits not bytes
        (arr = buffers[enum_js_1.BufferType.DATA]) && (buffers[enum_js_1.BufferType.DATA] = typeId === 6 ? arr : arr.subarray(stride * offset, stride * (offset + length)));
        return buffers;
      }
      _sliceChildren(children, offset, length) {
        return children.map((child) => child.slice(offset, length));
      }
    };
    exports2.Data = Data;
    Data.prototype.children = Object.freeze([]);
    var visitor_js_1 = require_visitor();
    var buffer_js_1 = require_buffer();
    var MakeDataVisitor = class _MakeDataVisitor extends visitor_js_1.Visitor {
      visit(props) {
        return this.getVisitFn(props["type"]).call(this, props);
      }
      visitNull(props) {
        const { ["type"]: type, ["offset"]: offset = 0, ["length"]: length = 0 } = props;
        return new Data(type, offset, length, length);
      }
      visitBool(props) {
        const { ["type"]: type, ["offset"]: offset = 0 } = props;
        const nullBitmap = (0, buffer_js_1.toUint8Array)(props["nullBitmap"]);
        const data = (0, buffer_js_1.toArrayBufferView)(type.ArrayType, props["data"]);
        const { ["length"]: length = data.length >> 3, ["nullCount"]: nullCount = props["nullBitmap"] ? -1 : 0 } = props;
        return new Data(type, offset, length, nullCount, [void 0, data, nullBitmap]);
      }
      visitInt(props) {
        const { ["type"]: type, ["offset"]: offset = 0 } = props;
        const nullBitmap = (0, buffer_js_1.toUint8Array)(props["nullBitmap"]);
        const data = (0, buffer_js_1.toArrayBufferView)(type.ArrayType, props["data"]);
        const { ["length"]: length = data.length, ["nullCount"]: nullCount = props["nullBitmap"] ? -1 : 0 } = props;
        return new Data(type, offset, length, nullCount, [void 0, data, nullBitmap]);
      }
      visitFloat(props) {
        const { ["type"]: type, ["offset"]: offset = 0 } = props;
        const nullBitmap = (0, buffer_js_1.toUint8Array)(props["nullBitmap"]);
        const data = (0, buffer_js_1.toArrayBufferView)(type.ArrayType, props["data"]);
        const { ["length"]: length = data.length, ["nullCount"]: nullCount = props["nullBitmap"] ? -1 : 0 } = props;
        return new Data(type, offset, length, nullCount, [void 0, data, nullBitmap]);
      }
      visitUtf8(props) {
        const { ["type"]: type, ["offset"]: offset = 0 } = props;
        const data = (0, buffer_js_1.toUint8Array)(props["data"]);
        const nullBitmap = (0, buffer_js_1.toUint8Array)(props["nullBitmap"]);
        const valueOffsets = (0, buffer_js_1.toInt32Array)(props["valueOffsets"]);
        const { ["length"]: length = valueOffsets.length - 1, ["nullCount"]: nullCount = props["nullBitmap"] ? -1 : 0 } = props;
        return new Data(type, offset, length, nullCount, [valueOffsets, data, nullBitmap]);
      }
      visitLargeUtf8(props) {
        const { ["type"]: type, ["offset"]: offset = 0 } = props;
        const data = (0, buffer_js_1.toUint8Array)(props["data"]);
        const nullBitmap = (0, buffer_js_1.toUint8Array)(props["nullBitmap"]);
        const valueOffsets = (0, buffer_js_1.toBigInt64Array)(props["valueOffsets"]);
        const { ["length"]: length = valueOffsets.length - 1, ["nullCount"]: nullCount = props["nullBitmap"] ? -1 : 0 } = props;
        return new Data(type, offset, length, nullCount, [valueOffsets, data, nullBitmap]);
      }
      visitBinary(props) {
        const { ["type"]: type, ["offset"]: offset = 0 } = props;
        const data = (0, buffer_js_1.toUint8Array)(props["data"]);
        const nullBitmap = (0, buffer_js_1.toUint8Array)(props["nullBitmap"]);
        const valueOffsets = (0, buffer_js_1.toInt32Array)(props["valueOffsets"]);
        const { ["length"]: length = valueOffsets.length - 1, ["nullCount"]: nullCount = props["nullBitmap"] ? -1 : 0 } = props;
        return new Data(type, offset, length, nullCount, [valueOffsets, data, nullBitmap]);
      }
      visitLargeBinary(props) {
        const { ["type"]: type, ["offset"]: offset = 0 } = props;
        const data = (0, buffer_js_1.toUint8Array)(props["data"]);
        const nullBitmap = (0, buffer_js_1.toUint8Array)(props["nullBitmap"]);
        const valueOffsets = (0, buffer_js_1.toBigInt64Array)(props["valueOffsets"]);
        const { ["length"]: length = valueOffsets.length - 1, ["nullCount"]: nullCount = props["nullBitmap"] ? -1 : 0 } = props;
        return new Data(type, offset, length, nullCount, [valueOffsets, data, nullBitmap]);
      }
      visitFixedSizeBinary(props) {
        const { ["type"]: type, ["offset"]: offset = 0 } = props;
        const nullBitmap = (0, buffer_js_1.toUint8Array)(props["nullBitmap"]);
        const data = (0, buffer_js_1.toArrayBufferView)(type.ArrayType, props["data"]);
        const { ["length"]: length = data.length / (0, type_js_1.strideForType)(type), ["nullCount"]: nullCount = props["nullBitmap"] ? -1 : 0 } = props;
        return new Data(type, offset, length, nullCount, [void 0, data, nullBitmap]);
      }
      visitDate(props) {
        const { ["type"]: type, ["offset"]: offset = 0 } = props;
        const nullBitmap = (0, buffer_js_1.toUint8Array)(props["nullBitmap"]);
        const data = (0, buffer_js_1.toArrayBufferView)(type.ArrayType, props["data"]);
        const { ["length"]: length = data.length / (0, type_js_1.strideForType)(type), ["nullCount"]: nullCount = props["nullBitmap"] ? -1 : 0 } = props;
        return new Data(type, offset, length, nullCount, [void 0, data, nullBitmap]);
      }
      visitTimestamp(props) {
        const { ["type"]: type, ["offset"]: offset = 0 } = props;
        const nullBitmap = (0, buffer_js_1.toUint8Array)(props["nullBitmap"]);
        const data = (0, buffer_js_1.toArrayBufferView)(type.ArrayType, props["data"]);
        const { ["length"]: length = data.length / (0, type_js_1.strideForType)(type), ["nullCount"]: nullCount = props["nullBitmap"] ? -1 : 0 } = props;
        return new Data(type, offset, length, nullCount, [void 0, data, nullBitmap]);
      }
      visitTime(props) {
        const { ["type"]: type, ["offset"]: offset = 0 } = props;
        const nullBitmap = (0, buffer_js_1.toUint8Array)(props["nullBitmap"]);
        const data = (0, buffer_js_1.toArrayBufferView)(type.ArrayType, props["data"]);
        const { ["length"]: length = data.length / (0, type_js_1.strideForType)(type), ["nullCount"]: nullCount = props["nullBitmap"] ? -1 : 0 } = props;
        return new Data(type, offset, length, nullCount, [void 0, data, nullBitmap]);
      }
      visitDecimal(props) {
        const { ["type"]: type, ["offset"]: offset = 0 } = props;
        const nullBitmap = (0, buffer_js_1.toUint8Array)(props["nullBitmap"]);
        const data = (0, buffer_js_1.toArrayBufferView)(type.ArrayType, props["data"]);
        const { ["length"]: length = data.length / (0, type_js_1.strideForType)(type), ["nullCount"]: nullCount = props["nullBitmap"] ? -1 : 0 } = props;
        return new Data(type, offset, length, nullCount, [void 0, data, nullBitmap]);
      }
      visitList(props) {
        const { ["type"]: type, ["offset"]: offset = 0, ["child"]: child } = props;
        const nullBitmap = (0, buffer_js_1.toUint8Array)(props["nullBitmap"]);
        const valueOffsets = (0, buffer_js_1.toInt32Array)(props["valueOffsets"]);
        const { ["length"]: length = valueOffsets.length - 1, ["nullCount"]: nullCount = props["nullBitmap"] ? -1 : 0 } = props;
        return new Data(type, offset, length, nullCount, [valueOffsets, void 0, nullBitmap], [child]);
      }
      visitStruct(props) {
        const { ["type"]: type, ["offset"]: offset = 0, ["children"]: children = [] } = props;
        const nullBitmap = (0, buffer_js_1.toUint8Array)(props["nullBitmap"]);
        const { length = children.reduce((len, { length: length2 }) => Math.max(len, length2), 0), nullCount = props["nullBitmap"] ? -1 : 0 } = props;
        return new Data(type, offset, length, nullCount, [void 0, void 0, nullBitmap], children);
      }
      visitUnion(props) {
        const { ["type"]: type, ["offset"]: offset = 0, ["children"]: children = [] } = props;
        const typeIds = (0, buffer_js_1.toArrayBufferView)(type.ArrayType, props["typeIds"]);
        const { ["length"]: length = typeIds.length, ["nullCount"]: nullCount = -1 } = props;
        if (type_js_1.DataType.isSparseUnion(type)) {
          return new Data(type, offset, length, nullCount, [void 0, void 0, void 0, typeIds], children);
        }
        const valueOffsets = (0, buffer_js_1.toInt32Array)(props["valueOffsets"]);
        return new Data(type, offset, length, nullCount, [valueOffsets, void 0, void 0, typeIds], children);
      }
      visitDictionary(props) {
        const { ["type"]: type, ["offset"]: offset = 0 } = props;
        const nullBitmap = (0, buffer_js_1.toUint8Array)(props["nullBitmap"]);
        const data = (0, buffer_js_1.toArrayBufferView)(type.indices.ArrayType, props["data"]);
        const { ["dictionary"]: dictionary = new vector_js_1.Vector([new _MakeDataVisitor().visit({ type: type.dictionary })]) } = props;
        const { ["length"]: length = data.length, ["nullCount"]: nullCount = props["nullBitmap"] ? -1 : 0 } = props;
        return new Data(type, offset, length, nullCount, [void 0, data, nullBitmap], [], dictionary);
      }
      visitInterval(props) {
        const { ["type"]: type, ["offset"]: offset = 0 } = props;
        const nullBitmap = (0, buffer_js_1.toUint8Array)(props["nullBitmap"]);
        const data = (0, buffer_js_1.toArrayBufferView)(type.ArrayType, props["data"]);
        const { ["length"]: length = data.length / (0, type_js_1.strideForType)(type), ["nullCount"]: nullCount = props["nullBitmap"] ? -1 : 0 } = props;
        return new Data(type, offset, length, nullCount, [void 0, data, nullBitmap]);
      }
      visitDuration(props) {
        const { ["type"]: type, ["offset"]: offset = 0 } = props;
        const nullBitmap = (0, buffer_js_1.toUint8Array)(props["nullBitmap"]);
        const data = (0, buffer_js_1.toArrayBufferView)(type.ArrayType, props["data"]);
        const { ["length"]: length = data.length, ["nullCount"]: nullCount = props["nullBitmap"] ? -1 : 0 } = props;
        return new Data(type, offset, length, nullCount, [void 0, data, nullBitmap]);
      }
      visitFixedSizeList(props) {
        const { ["type"]: type, ["offset"]: offset = 0, ["child"]: child = new _MakeDataVisitor().visit({ type: type.valueType }) } = props;
        const nullBitmap = (0, buffer_js_1.toUint8Array)(props["nullBitmap"]);
        const { ["length"]: length = child.length / (0, type_js_1.strideForType)(type), ["nullCount"]: nullCount = props["nullBitmap"] ? -1 : 0 } = props;
        return new Data(type, offset, length, nullCount, [void 0, void 0, nullBitmap], [child]);
      }
      visitMap(props) {
        const { ["type"]: type, ["offset"]: offset = 0, ["child"]: child = new _MakeDataVisitor().visit({ type: type.childType }) } = props;
        const nullBitmap = (0, buffer_js_1.toUint8Array)(props["nullBitmap"]);
        const valueOffsets = (0, buffer_js_1.toInt32Array)(props["valueOffsets"]);
        const { ["length"]: length = valueOffsets.length - 1, ["nullCount"]: nullCount = props["nullBitmap"] ? -1 : 0 } = props;
        return new Data(type, offset, length, nullCount, [valueOffsets, void 0, nullBitmap], [child]);
      }
    };
    var makeDataVisitor = new MakeDataVisitor();
    function makeData(props) {
      return makeDataVisitor.visit(props);
    }
    exports2.makeData = makeData;
  }
});

// node_modules/apache-arrow/util/chunk.js
var require_chunk = __commonJS({
  "node_modules/apache-arrow/util/chunk.js"(exports2) {
    "use strict";
    Object.defineProperty(exports2, "__esModule", { value: true });
    exports2.wrapChunkedIndexOf = exports2.wrapChunkedCall2 = exports2.wrapChunkedCall1 = exports2.isChunkedValid = exports2.binarySearch = exports2.sliceChunks = exports2.computeChunkOffsets = exports2.computeChunkNullCounts = exports2.computeChunkNullable = exports2.ChunkedIterator = void 0;
    var ChunkedIterator = class {
      constructor(numChunks = 0, getChunkIterator) {
        this.numChunks = numChunks;
        this.getChunkIterator = getChunkIterator;
        this.chunkIndex = 0;
        this.chunkIterator = this.getChunkIterator(0);
      }
      next() {
        while (this.chunkIndex < this.numChunks) {
          const next = this.chunkIterator.next();
          if (!next.done) {
            return next;
          }
          if (++this.chunkIndex < this.numChunks) {
            this.chunkIterator = this.getChunkIterator(this.chunkIndex);
          }
        }
        return { done: true, value: null };
      }
      [Symbol.iterator]() {
        return this;
      }
    };
    exports2.ChunkedIterator = ChunkedIterator;
    function computeChunkNullable(chunks) {
      return chunks.some((chunk) => chunk.nullable);
    }
    exports2.computeChunkNullable = computeChunkNullable;
    function computeChunkNullCounts(chunks) {
      return chunks.reduce((nullCount, chunk) => nullCount + chunk.nullCount, 0);
    }
    exports2.computeChunkNullCounts = computeChunkNullCounts;
    function computeChunkOffsets(chunks) {
      return chunks.reduce((offsets, chunk, index) => {
        offsets[index + 1] = offsets[index] + chunk.length;
        return offsets;
      }, new Uint32Array(chunks.length + 1));
    }
    exports2.computeChunkOffsets = computeChunkOffsets;
    function sliceChunks(chunks, offsets, begin, end) {
      const slices = [];
      for (let i = -1, n = chunks.length; ++i < n; ) {
        const chunk = chunks[i];
        const offset = offsets[i];
        const { length } = chunk;
        if (offset >= end) {
          break;
        }
        if (begin >= offset + length) {
          continue;
        }
        if (offset >= begin && offset + length <= end) {
          slices.push(chunk);
          continue;
        }
        const from = Math.max(0, begin - offset);
        const to = Math.min(end - offset, length);
        slices.push(chunk.slice(from, to - from));
      }
      if (slices.length === 0) {
        slices.push(chunks[0].slice(0, 0));
      }
      return slices;
    }
    exports2.sliceChunks = sliceChunks;
    function binarySearch(chunks, offsets, idx, fn) {
      let lhs = 0, mid = 0, rhs = offsets.length - 1;
      do {
        if (lhs >= rhs - 1) {
          return idx < offsets[rhs] ? fn(chunks, lhs, idx - offsets[lhs]) : null;
        }
        mid = lhs + Math.trunc((rhs - lhs) * 0.5);
        idx < offsets[mid] ? rhs = mid : lhs = mid;
      } while (lhs < rhs);
    }
    exports2.binarySearch = binarySearch;
    function isChunkedValid(data, index) {
      return data.getValid(index);
    }
    exports2.isChunkedValid = isChunkedValid;
    function wrapChunkedCall1(fn) {
      function chunkedFn(chunks, i, j) {
        return fn(chunks[i], j);
      }
      return function(index) {
        const data = this.data;
        return binarySearch(data, this._offsets, index, chunkedFn);
      };
    }
    exports2.wrapChunkedCall1 = wrapChunkedCall1;
    function wrapChunkedCall2(fn) {
      let _2;
      function chunkedFn(chunks, i, j) {
        return fn(chunks[i], j, _2);
      }
      return function(index, value) {
        const data = this.data;
        _2 = value;
        const result = binarySearch(data, this._offsets, index, chunkedFn);
        _2 = void 0;
        return result;
      };
    }
    exports2.wrapChunkedCall2 = wrapChunkedCall2;
    function wrapChunkedIndexOf(indexOf) {
      let _1;
      function chunkedIndexOf(data, chunkIndex, fromIndex) {
        let begin = fromIndex, index = 0, total = 0;
        for (let i = chunkIndex - 1, n = data.length; ++i < n; ) {
          const chunk = data[i];
          if (~(index = indexOf(chunk, _1, begin))) {
            return total + index;
          }
          begin = 0;
          total += chunk.length;
        }
        return -1;
      }
      return function(element, offset) {
        _1 = element;
        const data = this.data;
        const result = typeof offset !== "number" ? chunkedIndexOf(data, 0, 0) : binarySearch(data, this._offsets, offset, chunkedIndexOf);
        _1 = void 0;
        return result;
      };
    }
    exports2.wrapChunkedIndexOf = wrapChunkedIndexOf;
  }
});

// node_modules/apache-arrow/visitor/indexof.js
var require_indexof = __commonJS({
  "node_modules/apache-arrow/visitor/indexof.js"(exports2) {
    "use strict";
    Object.defineProperty(exports2, "__esModule", { value: true });
    exports2.instance = exports2.IndexOfVisitor = void 0;
    var enum_js_1 = require_enum();
    var visitor_js_1 = require_visitor();
    var get_js_1 = require_get();
    var bit_js_1 = require_bit();
    var vector_js_1 = require_vector();
    var IndexOfVisitor = class extends visitor_js_1.Visitor {
    };
    exports2.IndexOfVisitor = IndexOfVisitor;
    function nullIndexOf(data, searchElement) {
      return searchElement === null && data.length > 0 ? 0 : -1;
    }
    function indexOfNull(data, fromIndex) {
      const { nullBitmap } = data;
      if (!nullBitmap || data.nullCount <= 0) {
        return -1;
      }
      let i = 0;
      for (const isValid of new bit_js_1.BitIterator(nullBitmap, data.offset + (fromIndex || 0), data.length, nullBitmap, bit_js_1.getBool)) {
        if (!isValid) {
          return i;
        }
        ++i;
      }
      return -1;
    }
    function indexOfValue(data, searchElement, fromIndex) {
      if (searchElement === void 0) {
        return -1;
      }
      if (searchElement === null) {
        switch (data.typeId) {
          // Unions don't have a nullBitmap of its own, so compare the `searchElement` to `get()`.
          case enum_js_1.Type.Union:
            break;
          // Dictionaries do have a nullBitmap, but their dictionary could also have null elements.
          case enum_js_1.Type.Dictionary:
            break;
          // All other types can iterate the null bitmap
          default:
            return indexOfNull(data, fromIndex);
        }
      }
      const get = get_js_1.instance.getVisitFn(data);
      const compare = (0, vector_js_1.createElementComparator)(searchElement);
      for (let i = (fromIndex || 0) - 1, n = data.length; ++i < n; ) {
        if (compare(get(data, i))) {
          return i;
        }
      }
      return -1;
    }
    function indexOfUnion(data, searchElement, fromIndex) {
      const get = get_js_1.instance.getVisitFn(data);
      const compare = (0, vector_js_1.createElementComparator)(searchElement);
      for (let i = (fromIndex || 0) - 1, n = data.length; ++i < n; ) {
        if (compare(get(data, i))) {
          return i;
        }
      }
      return -1;
    }
    IndexOfVisitor.prototype.visitNull = nullIndexOf;
    IndexOfVisitor.prototype.visitBool = indexOfValue;
    IndexOfVisitor.prototype.visitInt = indexOfValue;
    IndexOfVisitor.prototype.visitInt8 = indexOfValue;
    IndexOfVisitor.prototype.visitInt16 = indexOfValue;
    IndexOfVisitor.prototype.visitInt32 = indexOfValue;
    IndexOfVisitor.prototype.visitInt64 = indexOfValue;
    IndexOfVisitor.prototype.visitUint8 = indexOfValue;
    IndexOfVisitor.prototype.visitUint16 = indexOfValue;
    IndexOfVisitor.prototype.visitUint32 = indexOfValue;
    IndexOfVisitor.prototype.visitUint64 = indexOfValue;
    IndexOfVisitor.prototype.visitFloat = indexOfValue;
    IndexOfVisitor.prototype.visitFloat16 = indexOfValue;
    IndexOfVisitor.prototype.visitFloat32 = indexOfValue;
    IndexOfVisitor.prototype.visitFloat64 = indexOfValue;
    IndexOfVisitor.prototype.visitUtf8 = indexOfValue;
    IndexOfVisitor.prototype.visitLargeUtf8 = indexOfValue;
    IndexOfVisitor.prototype.visitBinary = indexOfValue;
    IndexOfVisitor.prototype.visitLargeBinary = indexOfValue;
    IndexOfVisitor.prototype.visitFixedSizeBinary = indexOfValue;
    IndexOfVisitor.prototype.visitDate = indexOfValue;
    IndexOfVisitor.prototype.visitDateDay = indexOfValue;
    IndexOfVisitor.prototype.visitDateMillisecond = indexOfValue;
    IndexOfVisitor.prototype.visitTimestamp = indexOfValue;
    IndexOfVisitor.prototype.visitTimestampSecond = indexOfValue;
    IndexOfVisitor.prototype.visitTimestampMillisecond = indexOfValue;
    IndexOfVisitor.prototype.visitTimestampMicrosecond = indexOfValue;
    IndexOfVisitor.prototype.visitTimestampNanosecond = indexOfValue;
    IndexOfVisitor.prototype.visitTime = indexOfValue;
    IndexOfVisitor.prototype.visitTimeSecond = indexOfValue;
    IndexOfVisitor.prototype.visitTimeMillisecond = indexOfValue;
    IndexOfVisitor.prototype.visitTimeMicrosecond = indexOfValue;
    IndexOfVisitor.prototype.visitTimeNanosecond = indexOfValue;
    IndexOfVisitor.prototype.visitDecimal = indexOfValue;
    IndexOfVisitor.prototype.visitList = indexOfValue;
    IndexOfVisitor.prototype.visitStruct = indexOfValue;
    IndexOfVisitor.prototype.visitUnion = indexOfValue;
    IndexOfVisitor.prototype.visitDenseUnion = indexOfUnion;
    IndexOfVisitor.prototype.visitSparseUnion = indexOfUnion;
    IndexOfVisitor.prototype.visitDictionary = indexOfValue;
    IndexOfVisitor.prototype.visitInterval = indexOfValue;
    IndexOfVisitor.prototype.visitIntervalDayTime = indexOfValue;
    IndexOfVisitor.prototype.visitIntervalYearMonth = indexOfValue;
    IndexOfVisitor.prototype.visitDuration = indexOfValue;
    IndexOfVisitor.prototype.visitDurationSecond = indexOfValue;
    IndexOfVisitor.prototype.visitDurationMillisecond = indexOfValue;
    IndexOfVisitor.prototype.visitDurationMicrosecond = indexOfValue;
    IndexOfVisitor.prototype.visitDurationNanosecond = indexOfValue;
    IndexOfVisitor.prototype.visitFixedSizeList = indexOfValue;
    IndexOfVisitor.prototype.visitMap = indexOfValue;
    exports2.instance = new IndexOfVisitor();
  }
});

// node_modules/apache-arrow/visitor/iterator.js
var require_iterator = __commonJS({
  "node_modules/apache-arrow/visitor/iterator.js"(exports2) {
    "use strict";
    Object.defineProperty(exports2, "__esModule", { value: true });
    exports2.instance = exports2.IteratorVisitor = void 0;
    var visitor_js_1 = require_visitor();
    var enum_js_1 = require_enum();
    var type_js_1 = require_type2();
    var chunk_js_1 = require_chunk();
    var IteratorVisitor = class extends visitor_js_1.Visitor {
    };
    exports2.IteratorVisitor = IteratorVisitor;
    function vectorIterator(vector) {
      const { type } = vector;
      if (vector.nullCount === 0 && vector.stride === 1 && // Don't defer to native iterator for timestamps since Numbers are expected
      // (DataType.isTimestamp(type)) && type.unit === TimeUnit.MILLISECOND ||
      (type_js_1.DataType.isInt(type) && type.bitWidth !== 64 || type_js_1.DataType.isTime(type) && type.bitWidth !== 64 || type_js_1.DataType.isFloat(type) && type.precision !== enum_js_1.Precision.HALF)) {
        return new chunk_js_1.ChunkedIterator(vector.data.length, (chunkIndex) => {
          const data = vector.data[chunkIndex];
          return data.values.subarray(0, data.length)[Symbol.iterator]();
        });
      }
      let offset = 0;
      return new chunk_js_1.ChunkedIterator(vector.data.length, (chunkIndex) => {
        const data = vector.data[chunkIndex];
        const length = data.length;
        const inner = vector.slice(offset, offset + length);
        offset += length;
        return new VectorIterator(inner);
      });
    }
    var VectorIterator = class {
      constructor(vector) {
        this.vector = vector;
        this.index = 0;
      }
      next() {
        if (this.index < this.vector.length) {
          return {
            value: this.vector.get(this.index++)
          };
        }
        return { done: true, value: null };
      }
      [Symbol.iterator]() {
        return this;
      }
    };
    IteratorVisitor.prototype.visitNull = vectorIterator;
    IteratorVisitor.prototype.visitBool = vectorIterator;
    IteratorVisitor.prototype.visitInt = vectorIterator;
    IteratorVisitor.prototype.visitInt8 = vectorIterator;
    IteratorVisitor.prototype.visitInt16 = vectorIterator;
    IteratorVisitor.prototype.visitInt32 = vectorIterator;
    IteratorVisitor.prototype.visitInt64 = vectorIterator;
    IteratorVisitor.prototype.visitUint8 = vectorIterator;
    IteratorVisitor.prototype.visitUint16 = vectorIterator;
    IteratorVisitor.prototype.visitUint32 = vectorIterator;
    IteratorVisitor.prototype.visitUint64 = vectorIterator;
    IteratorVisitor.prototype.visitFloat = vectorIterator;
    IteratorVisitor.prototype.visitFloat16 = vectorIterator;
    IteratorVisitor.prototype.visitFloat32 = vectorIterator;
    IteratorVisitor.prototype.visitFloat64 = vectorIterator;
    IteratorVisitor.prototype.visitUtf8 = vectorIterator;
    IteratorVisitor.prototype.visitLargeUtf8 = vectorIterator;
    IteratorVisitor.prototype.visitBinary = vectorIterator;
    IteratorVisitor.prototype.visitLargeBinary = vectorIterator;
    IteratorVisitor.prototype.visitFixedSizeBinary = vectorIterator;
    IteratorVisitor.prototype.visitDate = vectorIterator;
    IteratorVisitor.prototype.visitDateDay = vectorIterator;
    IteratorVisitor.prototype.visitDateMillisecond = vectorIterator;
    IteratorVisitor.prototype.visitTimestamp = vectorIterator;
    IteratorVisitor.prototype.visitTimestampSecond = vectorIterator;
    IteratorVisitor.prototype.visitTimestampMillisecond = vectorIterator;
    IteratorVisitor.prototype.visitTimestampMicrosecond = vectorIterator;
    IteratorVisitor.prototype.visitTimestampNanosecond = vectorIterator;
    IteratorVisitor.prototype.visitTime = vectorIterator;
    IteratorVisitor.prototype.visitTimeSecond = vectorIterator;
    IteratorVisitor.prototype.visitTimeMillisecond = vectorIterator;
    IteratorVisitor.prototype.visitTimeMicrosecond = vectorIterator;
    IteratorVisitor.prototype.visitTimeNanosecond = vectorIterator;
    IteratorVisitor.prototype.visitDecimal = vectorIterator;
    IteratorVisitor.prototype.visitList = vectorIterator;
    IteratorVisitor.prototype.visitStruct = vectorIterator;
    IteratorVisitor.prototype.visitUnion = vectorIterator;
    IteratorVisitor.prototype.visitDenseUnion = vectorIterator;
    IteratorVisitor.prototype.visitSparseUnion = vectorIterator;
    IteratorVisitor.prototype.visitDictionary = vectorIterator;
    IteratorVisitor.prototype.visitInterval = vectorIterator;
    IteratorVisitor.prototype.visitIntervalDayTime = vectorIterator;
    IteratorVisitor.prototype.visitIntervalYearMonth = vectorIterator;
    IteratorVisitor.prototype.visitDuration = vectorIterator;
    IteratorVisitor.prototype.visitDurationSecond = vectorIterator;
    IteratorVisitor.prototype.visitDurationMillisecond = vectorIterator;
    IteratorVisitor.prototype.visitDurationMicrosecond = vectorIterator;
    IteratorVisitor.prototype.visitDurationNanosecond = vectorIterator;
    IteratorVisitor.prototype.visitFixedSizeList = vectorIterator;
    IteratorVisitor.prototype.visitMap = vectorIterator;
    exports2.instance = new IteratorVisitor();
  }
});

// node_modules/apache-arrow/vector.js
var require_vector2 = __commonJS({
  "node_modules/apache-arrow/vector.js"(exports2) {
    "use strict";
    var _a;
    Object.defineProperty(exports2, "__esModule", { value: true });
    exports2.makeVector = exports2.Vector = void 0;
    var enum_js_1 = require_enum();
    var vector_js_1 = require_vector();
    var type_js_1 = require_type2();
    var data_js_1 = require_data();
    var chunk_js_1 = require_chunk();
    var get_js_1 = require_get();
    var set_js_1 = require_set();
    var indexof_js_1 = require_indexof();
    var iterator_js_1 = require_iterator();
    var visitorsByTypeId = {};
    var vectorPrototypesByTypeId = {};
    var Vector = class _Vector {
      constructor(input) {
        var _b, _c, _d;
        const data = input[0] instanceof _Vector ? input.flatMap((x) => x.data) : input;
        if (data.length === 0 || data.some((x) => !(x instanceof data_js_1.Data))) {
          throw new TypeError("Vector constructor expects an Array of Data instances.");
        }
        const type = (_b = data[0]) === null || _b === void 0 ? void 0 : _b.type;
        switch (data.length) {
          case 0:
            this._offsets = [0];
            break;
          case 1: {
            const { get, set, indexOf } = visitorsByTypeId[type.typeId];
            const unchunkedData = data[0];
            this.isValid = (index) => (0, chunk_js_1.isChunkedValid)(unchunkedData, index);
            this.get = (index) => get(unchunkedData, index);
            this.set = (index, value) => set(unchunkedData, index, value);
            this.indexOf = (index) => indexOf(unchunkedData, index);
            this._offsets = [0, unchunkedData.length];
            break;
          }
          default:
            Object.setPrototypeOf(this, vectorPrototypesByTypeId[type.typeId]);
            this._offsets = (0, chunk_js_1.computeChunkOffsets)(data);
            break;
        }
        this.data = data;
        this.type = type;
        this.stride = (0, type_js_1.strideForType)(type);
        this.numChildren = (_d = (_c = type.children) === null || _c === void 0 ? void 0 : _c.length) !== null && _d !== void 0 ? _d : 0;
        this.length = this._offsets.at(-1);
      }
      /**
       * The aggregate size (in bytes) of this Vector's buffers and/or child Vectors.
       */
      get byteLength() {
        return this.data.reduce((byteLength, data) => byteLength + data.byteLength, 0);
      }
      /**
       * Whether this Vector's elements can contain null values.
       */
      get nullable() {
        return (0, chunk_js_1.computeChunkNullable)(this.data);
      }
      /**
       * The number of null elements in this Vector.
       */
      get nullCount() {
        return (0, chunk_js_1.computeChunkNullCounts)(this.data);
      }
      /**
       * The Array or TypedArray constructor used for the JS representation
       *  of the element's values in {@link Vector.prototype.toArray `toArray()`}.
       */
      get ArrayType() {
        return this.type.ArrayType;
      }
      /**
       * The name that should be printed when the Vector is logged in a message.
       */
      get [Symbol.toStringTag]() {
        return `${this.VectorName}<${this.type[Symbol.toStringTag]}>`;
      }
      /**
       * The name of this Vector.
       */
      get VectorName() {
        return `${enum_js_1.Type[this.type.typeId]}Vector`;
      }
      /**
       * Check whether an element is null.
       * @param index The index at which to read the validity bitmap.
       */
      // @ts-ignore
      isValid(index) {
        return false;
      }
      /**
       * Get an element value by position.
       * @param index The index of the element to read.
       */
      // @ts-ignore
      get(index) {
        return null;
      }
      /**
       * Get an element value by position.
       * @param index The index of the element to read. A negative index will count back from the last element.
       */
      at(index) {
        return this.get((0, vector_js_1.wrapIndex)(index, this.length));
      }
      /**
       * Set an element value by position.
       * @param index The index of the element to write.
       * @param value The value to set.
       */
      // @ts-ignore
      set(index, value) {
        return;
      }
      /**
       * Retrieve the index of the first occurrence of a value in an Vector.
       * @param element The value to locate in the Vector.
       * @param offset The index at which to begin the search. If offset is omitted, the search starts at index 0.
       */
      // @ts-ignore
      indexOf(element, offset) {
        return -1;
      }
      includes(element, offset) {
        return this.indexOf(element, offset) > -1;
      }
      /**
       * Iterator for the Vector's elements.
       */
      [Symbol.iterator]() {
        return iterator_js_1.instance.visit(this);
      }
      /**
       * Combines two or more Vectors of the same type.
       * @param others Additional Vectors to add to the end of this Vector.
       */
      concat(...others) {
        return new _Vector(this.data.concat(others.flatMap((x) => x.data).flat(Number.POSITIVE_INFINITY)));
      }
      /**
       * Return a zero-copy sub-section of this Vector.
       * @param start The beginning of the specified portion of the Vector.
       * @param end The end of the specified portion of the Vector. This is exclusive of the element at the index 'end'.
       */
      slice(begin, end) {
        return new _Vector((0, vector_js_1.clampRange)(this, begin, end, ({ data, _offsets }, begin2, end2) => (0, chunk_js_1.sliceChunks)(data, _offsets, begin2, end2)));
      }
      toJSON() {
        return [...this];
      }
      /**
       * Return a JavaScript Array or TypedArray of the Vector's elements.
       *
       * @note If this Vector contains a single Data chunk and the Vector's type is a
       *  primitive numeric type corresponding to one of the JavaScript TypedArrays, this
       *  method returns a zero-copy slice of the underlying TypedArray values. If there's
       *  more than one chunk, the resulting TypedArray will be a copy of the data from each
       *  chunk's underlying TypedArray values.
       *
       * @returns An Array or TypedArray of the Vector's elements, based on the Vector's DataType.
       */
      toArray() {
        const { type, data, length, stride, ArrayType } = this;
        switch (type.typeId) {
          case enum_js_1.Type.Int:
          case enum_js_1.Type.Float:
          case enum_js_1.Type.Decimal:
          case enum_js_1.Type.Time:
          case enum_js_1.Type.Timestamp:
            switch (data.length) {
              case 0:
                return new ArrayType();
              case 1:
                return data[0].values.subarray(0, length * stride);
              default:
                return data.reduce((memo, { values, length: chunk_length }) => {
                  memo.array.set(values.subarray(0, chunk_length * stride), memo.offset);
                  memo.offset += chunk_length * stride;
                  return memo;
                }, { array: new ArrayType(length * stride), offset: 0 }).array;
            }
        }
        return [...this];
      }
      /**
       * Returns a string representation of the Vector.
       *
       * @returns A string representation of the Vector.
       */
      toString() {
        return `[${[...this].join(",")}]`;
      }
      /**
       * Returns a child Vector by name, or null if this Vector has no child with the given name.
       * @param name The name of the child to retrieve.
       */
      getChild(name) {
        var _b;
        return this.getChildAt((_b = this.type.children) === null || _b === void 0 ? void 0 : _b.findIndex((f) => f.name === name));
      }
      /**
       * Returns a child Vector by index, or null if this Vector has no child at the supplied index.
       * @param index The index of the child to retrieve.
       */
      getChildAt(index) {
        if (index > -1 && index < this.numChildren) {
          return new _Vector(this.data.map(({ children }) => children[index]));
        }
        return null;
      }
      get isMemoized() {
        if (type_js_1.DataType.isDictionary(this.type)) {
          return this.data[0].dictionary.isMemoized;
        }
        return false;
      }
      /**
       * Adds memoization to the Vector's {@link get} method. For dictionary
       * vectors, this method return a vector that memoizes only the dictionary
       * values.
       *
       * Memoization is very useful when decoding a value is expensive such as
       * Utf8. The memoization creates a cache of the size of the Vector and
       * therefore increases memory usage.
       *
       * @returns A new vector that memoizes calls to {@link get}.
       */
      memoize() {
        if (type_js_1.DataType.isDictionary(this.type)) {
          const dictionary = new MemoizedVector(this.data[0].dictionary);
          const newData = this.data.map((data) => {
            const cloned = data.clone();
            cloned.dictionary = dictionary;
            return cloned;
          });
          return new _Vector(newData);
        }
        return new MemoizedVector(this);
      }
      /**
       * Returns a vector without memoization of the {@link get} method. If this
       * vector is not memoized, this method returns this vector.
       *
       * @returns A new vector without memoization.
       */
      unmemoize() {
        if (type_js_1.DataType.isDictionary(this.type) && this.isMemoized) {
          const dictionary = this.data[0].dictionary.unmemoize();
          const newData = this.data.map((data) => {
            const newData2 = data.clone();
            newData2.dictionary = dictionary;
            return newData2;
          });
          return new _Vector(newData);
        }
        return this;
      }
    };
    exports2.Vector = Vector;
    _a = Symbol.toStringTag;
    Vector[_a] = ((proto) => {
      proto.type = type_js_1.DataType.prototype;
      proto.data = [];
      proto.length = 0;
      proto.stride = 1;
      proto.numChildren = 0;
      proto._offsets = new Uint32Array([0]);
      proto[Symbol.isConcatSpreadable] = true;
      const typeIds = Object.keys(enum_js_1.Type).map((T) => enum_js_1.Type[T]).filter((T) => typeof T === "number" && T !== enum_js_1.Type.NONE);
      for (const typeId of typeIds) {
        const get = get_js_1.instance.getVisitFnByTypeId(typeId);
        const set = set_js_1.instance.getVisitFnByTypeId(typeId);
        const indexOf = indexof_js_1.instance.getVisitFnByTypeId(typeId);
        visitorsByTypeId[typeId] = { get, set, indexOf };
        vectorPrototypesByTypeId[typeId] = Object.create(proto, {
          ["isValid"]: { value: (0, chunk_js_1.wrapChunkedCall1)(chunk_js_1.isChunkedValid) },
          ["get"]: { value: (0, chunk_js_1.wrapChunkedCall1)(get_js_1.instance.getVisitFnByTypeId(typeId)) },
          ["set"]: { value: (0, chunk_js_1.wrapChunkedCall2)(set_js_1.instance.getVisitFnByTypeId(typeId)) },
          ["indexOf"]: { value: (0, chunk_js_1.wrapChunkedIndexOf)(indexof_js_1.instance.getVisitFnByTypeId(typeId)) }
        });
      }
      return "Vector";
    })(Vector.prototype);
    var MemoizedVector = class _MemoizedVector extends Vector {
      constructor(vector) {
        super(vector.data);
        const get = this.get;
        const set = this.set;
        const slice = this.slice;
        const cache = new Array(this.length);
        Object.defineProperty(this, "get", {
          value(index) {
            const cachedValue = cache[index];
            if (cachedValue !== void 0) {
              return cachedValue;
            }
            const value = get.call(this, index);
            cache[index] = value;
            return value;
          }
        });
        Object.defineProperty(this, "set", {
          value(index, value) {
            set.call(this, index, value);
            cache[index] = value;
          }
        });
        Object.defineProperty(this, "slice", {
          value: (begin, end) => new _MemoizedVector(slice.call(this, begin, end))
        });
        Object.defineProperty(this, "isMemoized", { value: true });
        Object.defineProperty(this, "unmemoize", {
          value: () => new Vector(this.data)
        });
        Object.defineProperty(this, "memoize", {
          value: () => this
        });
      }
    };
    var dtypes = require_type2();
    function makeVector(init) {
      if (init) {
        if (init instanceof data_js_1.Data) {
          return new Vector([init]);
        }
        if (init instanceof Vector) {
          return new Vector(init.data);
        }
        if (init.type instanceof type_js_1.DataType) {
          return new Vector([(0, data_js_1.makeData)(init)]);
        }
        if (Array.isArray(init)) {
          return new Vector(init.flatMap((v) => unwrapInputs(v)));
        }
        if (ArrayBuffer.isView(init)) {
          if (init instanceof DataView) {
            init = new Uint8Array(init.buffer);
          }
          const props = { offset: 0, length: init.length, nullCount: -1, data: init };
          if (init instanceof Int8Array) {
            return new Vector([(0, data_js_1.makeData)(Object.assign(Object.assign({}, props), { type: new dtypes.Int8() }))]);
          }
          if (init instanceof Int16Array) {
            return new Vector([(0, data_js_1.makeData)(Object.assign(Object.assign({}, props), { type: new dtypes.Int16() }))]);
          }
          if (init instanceof Int32Array) {
            return new Vector([(0, data_js_1.makeData)(Object.assign(Object.assign({}, props), { type: new dtypes.Int32() }))]);
          }
          if (init instanceof BigInt64Array) {
            return new Vector([(0, data_js_1.makeData)(Object.assign(Object.assign({}, props), { type: new dtypes.Int64() }))]);
          }
          if (init instanceof Uint8Array || init instanceof Uint8ClampedArray) {
            return new Vector([(0, data_js_1.makeData)(Object.assign(Object.assign({}, props), { type: new dtypes.Uint8() }))]);
          }
          if (init instanceof Uint16Array) {
            return new Vector([(0, data_js_1.makeData)(Object.assign(Object.assign({}, props), { type: new dtypes.Uint16() }))]);
          }
          if (init instanceof Uint32Array) {
            return new Vector([(0, data_js_1.makeData)(Object.assign(Object.assign({}, props), { type: new dtypes.Uint32() }))]);
          }
          if (init instanceof BigUint64Array) {
            return new Vector([(0, data_js_1.makeData)(Object.assign(Object.assign({}, props), { type: new dtypes.Uint64() }))]);
          }
          if (init instanceof Float32Array) {
            return new Vector([(0, data_js_1.makeData)(Object.assign(Object.assign({}, props), { type: new dtypes.Float32() }))]);
          }
          if (init instanceof Float64Array) {
            return new Vector([(0, data_js_1.makeData)(Object.assign(Object.assign({}, props), { type: new dtypes.Float64() }))]);
          }
          throw new Error("Unrecognized input");
        }
      }
      throw new Error("Unrecognized input");
    }
    exports2.makeVector = makeVector;
    function unwrapInputs(x) {
      return x instanceof data_js_1.Data ? [x] : x instanceof Vector ? x.data : makeVector(x).data;
    }
  }
});

// node_modules/apache-arrow/builder/valid.js
var require_valid = __commonJS({
  "node_modules/apache-arrow/builder/valid.js"(exports2) {
    "use strict";
    Object.defineProperty(exports2, "__esModule", { value: true });
    exports2.createIsValidFunction = void 0;
    var pretty_js_1 = require_pretty();
    function createIsValidFunction(nullValues) {
      if (!nullValues || nullValues.length <= 0) {
        return function isValid(value) {
          return true;
        };
      }
      let fnBody = "";
      const noNaNs = nullValues.filter((x) => x === x);
      if (noNaNs.length > 0) {
        fnBody = `
    switch (x) {${noNaNs.map((x) => `
        case ${valueToCase(x)}:`).join("")}
            return false;
    }`;
      }
      if (nullValues.length !== noNaNs.length) {
        fnBody = `if (x !== x) return false;
${fnBody}`;
      }
      return new Function(`x`, `${fnBody}
return true;`);
    }
    exports2.createIsValidFunction = createIsValidFunction;
    function valueToCase(x) {
      if (typeof x !== "bigint") {
        return (0, pretty_js_1.valueToString)(x);
      }
      return `${(0, pretty_js_1.valueToString)(x)}n`;
    }
  }
});

// node_modules/apache-arrow/builder/buffer.js
var require_buffer3 = __commonJS({
  "node_modules/apache-arrow/builder/buffer.js"(exports2) {
    "use strict";
    Object.defineProperty(exports2, "__esModule", { value: true });
    exports2.OffsetsBufferBuilder = exports2.BitmapBufferBuilder = exports2.DataBufferBuilder = exports2.BufferBuilder = void 0;
    var buffer_js_1 = require_buffer();
    function roundLengthUpToNearest64Bytes(len, BPE) {
      const bytesMinus1 = Math.ceil(len) * BPE - 1;
      return (bytesMinus1 - bytesMinus1 % 64 + 64 || 64) / BPE;
    }
    function resizeArray(arr, len = 0) {
      return arr.length >= len ? arr.subarray(0, len) : (0, buffer_js_1.memcpy)(new arr.constructor(len), arr, 0);
    }
    var BufferBuilder = class {
      constructor(bufferType, initialSize = 0, stride = 1) {
        this.length = Math.ceil(initialSize / stride);
        this.buffer = new bufferType(this.length);
        this.stride = stride;
        this.BYTES_PER_ELEMENT = bufferType.BYTES_PER_ELEMENT;
        this.ArrayType = bufferType;
      }
      get byteLength() {
        return Math.ceil(this.length * this.stride) * this.BYTES_PER_ELEMENT;
      }
      get reservedLength() {
        return this.buffer.length / this.stride;
      }
      get reservedByteLength() {
        return this.buffer.byteLength;
      }
      // @ts-ignore
      set(index, value) {
        return this;
      }
      append(value) {
        return this.set(this.length, value);
      }
      reserve(extra) {
        if (extra > 0) {
          this.length += extra;
          const stride = this.stride;
          const length = this.length * stride;
          const reserved = this.buffer.length;
          if (length >= reserved) {
            this._resize(reserved === 0 ? roundLengthUpToNearest64Bytes(length * 1, this.BYTES_PER_ELEMENT) : roundLengthUpToNearest64Bytes(length * 2, this.BYTES_PER_ELEMENT));
          }
        }
        return this;
      }
      flush(length = this.length) {
        length = roundLengthUpToNearest64Bytes(length * this.stride, this.BYTES_PER_ELEMENT);
        const array = resizeArray(this.buffer, length);
        this.clear();
        return array;
      }
      clear() {
        this.length = 0;
        this.buffer = new this.ArrayType();
        return this;
      }
      _resize(newLength) {
        return this.buffer = resizeArray(this.buffer, newLength);
      }
    };
    exports2.BufferBuilder = BufferBuilder;
    var DataBufferBuilder = class extends BufferBuilder {
      last() {
        return this.get(this.length - 1);
      }
      get(index) {
        return this.buffer[index];
      }
      set(index, value) {
        this.reserve(index - this.length + 1);
        this.buffer[index * this.stride] = value;
        return this;
      }
    };
    exports2.DataBufferBuilder = DataBufferBuilder;
    var BitmapBufferBuilder = class extends DataBufferBuilder {
      constructor() {
        super(Uint8Array, 0, 1 / 8);
        this.numValid = 0;
      }
      get numInvalid() {
        return this.length - this.numValid;
      }
      get(idx) {
        return this.buffer[idx >> 3] >> idx % 8 & 1;
      }
      set(idx, val) {
        const { buffer } = this.reserve(idx - this.length + 1);
        const byte = idx >> 3, bit = idx % 8, cur = buffer[byte] >> bit & 1;
        val ? cur === 0 && (buffer[byte] |= 1 << bit, ++this.numValid) : cur === 1 && (buffer[byte] &= ~(1 << bit), --this.numValid);
        return this;
      }
      clear() {
        this.numValid = 0;
        return super.clear();
      }
    };
    exports2.BitmapBufferBuilder = BitmapBufferBuilder;
    var OffsetsBufferBuilder = class extends DataBufferBuilder {
      constructor(type) {
        super(type.OffsetArrayType, 1, 1);
      }
      append(value) {
        return this.set(this.length - 1, value);
      }
      set(index, value) {
        const offset = this.length - 1;
        const buffer = this.reserve(index - offset + 1).buffer;
        if (offset < index++ && offset >= 0) {
          buffer.fill(buffer[offset], offset, index);
        }
        buffer[index] = buffer[index - 1] + value;
        return this;
      }
      flush(length = this.length - 1) {
        if (length > this.length) {
          this.set(length - 1, this.BYTES_PER_ELEMENT > 4 ? BigInt(0) : 0);
        }
        return super.flush(length + 1);
      }
    };
    exports2.OffsetsBufferBuilder = OffsetsBufferBuilder;
  }
});

// node_modules/apache-arrow/builder.js
var require_builder = __commonJS({
  "node_modules/apache-arrow/builder.js"(exports2) {
    "use strict";
    Object.defineProperty(exports2, "__esModule", { value: true });
    exports2.VariableWidthBuilder = exports2.FixedWidthBuilder = exports2.Builder = void 0;
    var vector_js_1 = require_vector2();
    var data_js_1 = require_data();
    var map_js_1 = require_map2();
    var type_js_1 = require_type2();
    var valid_js_1 = require_valid();
    var buffer_js_1 = require_buffer3();
    var Builder2 = class {
      /** @nocollapse */
      // @ts-ignore
      static throughNode(options) {
        throw new Error(`"throughNode" not available in this environment`);
      }
      /** @nocollapse */
      // @ts-ignore
      static throughDOM(options) {
        throw new Error(`"throughDOM" not available in this environment`);
      }
      /**
       * Construct a builder with the given Arrow DataType with optional null values,
       * which will be interpreted as "null" when set or appended to the `Builder`.
       * @param {{ type: T, nullValues?: any[] }} options A `BuilderOptions` object used to create this `Builder`.
       */
      constructor({ "type": type, "nullValues": nulls }) {
        this.length = 0;
        this.finished = false;
        this.type = type;
        this.children = [];
        this.nullValues = nulls;
        this.stride = (0, type_js_1.strideForType)(type);
        this._nulls = new buffer_js_1.BitmapBufferBuilder();
        if (nulls && nulls.length > 0) {
          this._isValid = (0, valid_js_1.createIsValidFunction)(nulls);
        }
      }
      /**
       * Flush the `Builder` and return a `Vector<T>`.
       * @returns {Vector<T>} A `Vector<T>` of the flushed values.
       */
      toVector() {
        return new vector_js_1.Vector([this.flush()]);
      }
      get ArrayType() {
        return this.type.ArrayType;
      }
      get nullCount() {
        return this._nulls.numInvalid;
      }
      get numChildren() {
        return this.children.length;
      }
      /**
       * @returns The aggregate length (in bytes) of the values that have been written.
       */
      get byteLength() {
        let size = 0;
        const { _offsets, _values, _nulls, _typeIds, children } = this;
        _offsets && (size += _offsets.byteLength);
        _values && (size += _values.byteLength);
        _nulls && (size += _nulls.byteLength);
        _typeIds && (size += _typeIds.byteLength);
        return children.reduce((size2, child) => size2 + child.byteLength, size);
      }
      /**
       * @returns The aggregate number of rows that have been reserved to write new values.
       */
      get reservedLength() {
        return this._nulls.reservedLength;
      }
      /**
       * @returns The aggregate length (in bytes) that has been reserved to write new values.
       */
      get reservedByteLength() {
        let size = 0;
        this._offsets && (size += this._offsets.reservedByteLength);
        this._values && (size += this._values.reservedByteLength);
        this._nulls && (size += this._nulls.reservedByteLength);
        this._typeIds && (size += this._typeIds.reservedByteLength);
        return this.children.reduce((size2, child) => size2 + child.reservedByteLength, size);
      }
      get valueOffsets() {
        return this._offsets ? this._offsets.buffer : null;
      }
      get values() {
        return this._values ? this._values.buffer : null;
      }
      get nullBitmap() {
        return this._nulls ? this._nulls.buffer : null;
      }
      get typeIds() {
        return this._typeIds ? this._typeIds.buffer : null;
      }
      /**
       * Appends a value (or null) to this `Builder`.
       * This is equivalent to `builder.set(builder.length, value)`.
       * @param {T['TValue'] | TNull } value The value to append.
       */
      append(value) {
        return this.set(this.length, value);
      }
      /**
       * Validates whether a value is valid (true), or null (false)
       * @param {T['TValue'] | TNull } value The value to compare against null the value representations
       */
      isValid(value) {
        return this._isValid(value);
      }
      /**
       * Write a value (or null-value sentinel) at the supplied index.
       * If the value matches one of the null-value representations, a 1-bit is
       * written to the null `BitmapBufferBuilder`. Otherwise, a 0 is written to
       * the null `BitmapBufferBuilder`, and the value is passed to
       * `Builder.prototype.setValue()`.
       * @param {number} index The index of the value to write.
       * @param {T['TValue'] | TNull } value The value to write at the supplied index.
       * @returns {this} The updated `Builder` instance.
       */
      set(index, value) {
        if (this.setValid(index, this.isValid(value))) {
          this.setValue(index, value);
        }
        return this;
      }
      /**
       * Write a value to the underlying buffers at the supplied index, bypassing
       * the null-value check. This is a low-level method that
       * @param {number} index
       * @param {T['TValue'] | TNull } value
       */
      setValue(index, value) {
        this._setValue(this, index, value);
      }
      setValid(index, valid) {
        this.length = this._nulls.set(index, +valid).length;
        return valid;
      }
      // @ts-ignore
      addChild(child, name = `${this.numChildren}`) {
        throw new Error(`Cannot append children to non-nested type "${this.type}"`);
      }
      /**
       * Retrieve the child `Builder` at the supplied `index`, or null if no child
       * exists at that index.
       * @param {number} index The index of the child `Builder` to retrieve.
       * @returns {Builder | null} The child Builder at the supplied index or null.
       */
      getChildAt(index) {
        return this.children[index] || null;
      }
      /**
       * Commit all the values that have been written to their underlying
       * ArrayBuffers, including any child Builders if applicable, and reset
       * the internal `Builder` state.
       * @returns A `Data<T>` of the buffers and children representing the values written.
       */
      flush() {
        let data;
        let typeIds;
        let nullBitmap;
        let valueOffsets;
        const { type, length, nullCount, _typeIds, _offsets, _values, _nulls } = this;
        if (typeIds = _typeIds === null || _typeIds === void 0 ? void 0 : _typeIds.flush(length)) {
          valueOffsets = _offsets === null || _offsets === void 0 ? void 0 : _offsets.flush(length);
        } else if (valueOffsets = _offsets === null || _offsets === void 0 ? void 0 : _offsets.flush(length)) {
          data = _values === null || _values === void 0 ? void 0 : _values.flush(_offsets.last());
        } else {
          data = _values === null || _values === void 0 ? void 0 : _values.flush(length);
        }
        if (nullCount > 0) {
          nullBitmap = _nulls === null || _nulls === void 0 ? void 0 : _nulls.flush(length);
        }
        const children = this.children.map((child) => child.flush());
        this.clear();
        return (0, data_js_1.makeData)({
          type,
          length,
          nullCount,
          children,
          "child": children[0],
          data,
          typeIds,
          nullBitmap,
          valueOffsets
        });
      }
      /**
       * Finalize this `Builder`, and child builders if applicable.
       * @returns {this} The finalized `Builder` instance.
       */
      finish() {
        this.finished = true;
        for (const child of this.children)
          child.finish();
        return this;
      }
      /**
       * Clear this Builder's internal state, including child Builders if applicable, and reset the length to 0.
       * @returns {this} The cleared `Builder` instance.
       */
      clear() {
        var _a, _b, _c, _d;
        this.length = 0;
        (_a = this._nulls) === null || _a === void 0 ? void 0 : _a.clear();
        (_b = this._values) === null || _b === void 0 ? void 0 : _b.clear();
        (_c = this._offsets) === null || _c === void 0 ? void 0 : _c.clear();
        (_d = this._typeIds) === null || _d === void 0 ? void 0 : _d.clear();
        for (const child of this.children)
          child.clear();
        return this;
      }
    };
    exports2.Builder = Builder2;
    Builder2.prototype.length = 1;
    Builder2.prototype.stride = 1;
    Builder2.prototype.children = null;
    Builder2.prototype.finished = false;
    Builder2.prototype.nullValues = null;
    Builder2.prototype._isValid = () => true;
    var FixedWidthBuilder = class extends Builder2 {
      constructor(opts) {
        super(opts);
        this._values = new buffer_js_1.DataBufferBuilder(this.ArrayType, 0, this.stride);
      }
      setValue(index, value) {
        const values = this._values;
        values.reserve(index - values.length + 1);
        return super.setValue(index, value);
      }
    };
    exports2.FixedWidthBuilder = FixedWidthBuilder;
    var VariableWidthBuilder = class extends Builder2 {
      constructor(opts) {
        super(opts);
        this._pendingLength = 0;
        this._offsets = new buffer_js_1.OffsetsBufferBuilder(opts.type);
      }
      setValue(index, value) {
        const pending = this._pending || (this._pending = /* @__PURE__ */ new Map());
        const current = pending.get(index);
        current && (this._pendingLength -= current.length);
        this._pendingLength += value instanceof map_js_1.MapRow ? value[map_js_1.kKeys].length : value.length;
        pending.set(index, value);
      }
      setValid(index, isValid) {
        if (!super.setValid(index, isValid)) {
          (this._pending || (this._pending = /* @__PURE__ */ new Map())).set(index, void 0);
          return false;
        }
        return true;
      }
      clear() {
        this._pendingLength = 0;
        this._pending = void 0;
        return super.clear();
      }
      flush() {
        this._flush();
        return super.flush();
      }
      finish() {
        this._flush();
        return super.finish();
      }
      _flush() {
        const pending = this._pending;
        const pendingLength = this._pendingLength;
        this._pendingLength = 0;
        this._pending = void 0;
        if (pending && pending.size > 0) {
          this._flushPending(pending, pendingLength);
        }
        return this;
      }
    };
    exports2.VariableWidthBuilder = VariableWidthBuilder;
  }
});

// node_modules/apache-arrow/fb/block.js
var require_block = __commonJS({
  "node_modules/apache-arrow/fb/block.js"(exports2) {
    "use strict";
    Object.defineProperty(exports2, "__esModule", { value: true });
    exports2.Block = void 0;
    var Block = class {
      constructor() {
        this.bb = null;
        this.bb_pos = 0;
      }
      __init(i, bb) {
        this.bb_pos = i;
        this.bb = bb;
        return this;
      }
      /**
       * Index to the start of the RecordBlock (note this is past the Message header)
       */
      offset() {
        return this.bb.readInt64(this.bb_pos);
      }
      /**
       * Length of the metadata
       */
      metaDataLength() {
        return this.bb.readInt32(this.bb_pos + 8);
      }
      /**
       * Length of the data (this is aligned so there can be a gap between this and
       * the metadata).
       */
      bodyLength() {
        return this.bb.readInt64(this.bb_pos + 16);
      }
      static sizeOf() {
        return 24;
      }
      static createBlock(builder, offset, metaDataLength, bodyLength) {
        builder.prep(8, 24);
        builder.writeInt64(BigInt(bodyLength !== null && bodyLength !== void 0 ? bodyLength : 0));
        builder.pad(4);
        builder.writeInt32(metaDataLength);
        builder.writeInt64(BigInt(offset !== null && offset !== void 0 ? offset : 0));
        return builder.offset();
      }
    };
    exports2.Block = Block;
  }
});

// node_modules/apache-arrow/fb/footer.js
var require_footer = __commonJS({
  "node_modules/apache-arrow/fb/footer.js"(exports2) {
    "use strict";
    Object.defineProperty(exports2, "__esModule", { value: true });
    exports2.Footer = void 0;
    var flatbuffers = (init_flatbuffers(), __toCommonJS(flatbuffers_exports));
    var block_js_1 = require_block();
    var key_value_js_1 = require_key_value();
    var metadata_version_js_1 = require_metadata_version();
    var schema_js_1 = require_schema();
    var Footer = class _Footer {
      constructor() {
        this.bb = null;
        this.bb_pos = 0;
      }
      __init(i, bb) {
        this.bb_pos = i;
        this.bb = bb;
        return this;
      }
      static getRootAsFooter(bb, obj) {
        return (obj || new _Footer()).__init(bb.readInt32(bb.position()) + bb.position(), bb);
      }
      static getSizePrefixedRootAsFooter(bb, obj) {
        bb.setPosition(bb.position() + flatbuffers.SIZE_PREFIX_LENGTH);
        return (obj || new _Footer()).__init(bb.readInt32(bb.position()) + bb.position(), bb);
      }
      version() {
        const offset = this.bb.__offset(this.bb_pos, 4);
        return offset ? this.bb.readInt16(this.bb_pos + offset) : metadata_version_js_1.MetadataVersion.V1;
      }
      schema(obj) {
        const offset = this.bb.__offset(this.bb_pos, 6);
        return offset ? (obj || new schema_js_1.Schema()).__init(this.bb.__indirect(this.bb_pos + offset), this.bb) : null;
      }
      dictionaries(index, obj) {
        const offset = this.bb.__offset(this.bb_pos, 8);
        return offset ? (obj || new block_js_1.Block()).__init(this.bb.__vector(this.bb_pos + offset) + index * 24, this.bb) : null;
      }
      dictionariesLength() {
        const offset = this.bb.__offset(this.bb_pos, 8);
        return offset ? this.bb.__vector_len(this.bb_pos + offset) : 0;
      }
      recordBatches(index, obj) {
        const offset = this.bb.__offset(this.bb_pos, 10);
        return offset ? (obj || new block_js_1.Block()).__init(this.bb.__vector(this.bb_pos + offset) + index * 24, this.bb) : null;
      }
      recordBatchesLength() {
        const offset = this.bb.__offset(this.bb_pos, 10);
        return offset ? this.bb.__vector_len(this.bb_pos + offset) : 0;
      }
      /**
       * User-defined metadata
       */
      customMetadata(index, obj) {
        const offset = this.bb.__offset(this.bb_pos, 12);
        return offset ? (obj || new key_value_js_1.KeyValue()).__init(this.bb.__indirect(this.bb.__vector(this.bb_pos + offset) + index * 4), this.bb) : null;
      }
      customMetadataLength() {
        const offset = this.bb.__offset(this.bb_pos, 12);
        return offset ? this.bb.__vector_len(this.bb_pos + offset) : 0;
      }
      static startFooter(builder) {
        builder.startObject(5);
      }
      static addVersion(builder, version) {
        builder.addFieldInt16(0, version, metadata_version_js_1.MetadataVersion.V1);
      }
      static addSchema(builder, schemaOffset) {
        builder.addFieldOffset(1, schemaOffset, 0);
      }
      static addDictionaries(builder, dictionariesOffset) {
        builder.addFieldOffset(2, dictionariesOffset, 0);
      }
      static startDictionariesVector(builder, numElems) {
        builder.startVector(24, numElems, 8);
      }
      static addRecordBatches(builder, recordBatchesOffset) {
        builder.addFieldOffset(3, recordBatchesOffset, 0);
      }
      static startRecordBatchesVector(builder, numElems) {
        builder.startVector(24, numElems, 8);
      }
      static addCustomMetadata(builder, customMetadataOffset) {
        builder.addFieldOffset(4, customMetadataOffset, 0);
      }
      static createCustomMetadataVector(builder, data) {
        builder.startVector(4, data.length, 4);
        for (let i = data.length - 1; i >= 0; i--) {
          builder.addOffset(data[i]);
        }
        return builder.endVector();
      }
      static startCustomMetadataVector(builder, numElems) {
        builder.startVector(4, numElems, 4);
      }
      static endFooter(builder) {
        const offset = builder.endObject();
        return offset;
      }
      static finishFooterBuffer(builder, offset) {
        builder.finish(offset);
      }
      static finishSizePrefixedFooterBuffer(builder, offset) {
        builder.finish(offset, void 0, true);
      }
    };
    exports2.Footer = Footer;
  }
});

// node_modules/apache-arrow/schema.js
var require_schema2 = __commonJS({
  "node_modules/apache-arrow/schema.js"(exports2) {
    "use strict";
    Object.defineProperty(exports2, "__esModule", { value: true });
    exports2.Field = exports2.Schema = void 0;
    var enum_js_1 = require_enum();
    var type_js_1 = require_type2();
    var Schema = class _Schema {
      constructor(fields = [], metadata, dictionaries, metadataVersion = enum_js_1.MetadataVersion.V5) {
        this.fields = fields || [];
        this.metadata = metadata || /* @__PURE__ */ new Map();
        if (!dictionaries) {
          dictionaries = generateDictionaryMap(this.fields);
        }
        this.dictionaries = dictionaries;
        this.metadataVersion = metadataVersion;
      }
      get [Symbol.toStringTag]() {
        return "Schema";
      }
      get names() {
        return this.fields.map((f) => f.name);
      }
      toString() {
        return `Schema<{ ${this.fields.map((f, i) => `${i}: ${f}`).join(", ")} }>`;
      }
      /**
       * Construct a new Schema containing only specified fields.
       *
       * @param fieldNames Names of fields to keep.
       * @returns A new Schema of fields matching the specified names.
       */
      select(fieldNames) {
        const names = new Set(fieldNames);
        const fields = this.fields.filter((f) => names.has(f.name));
        return new _Schema(fields, this.metadata);
      }
      /**
       * Construct a new Schema containing only fields at the specified indices.
       *
       * @param fieldIndices Indices of fields to keep.
       * @returns A new Schema of fields at the specified indices.
       */
      selectAt(fieldIndices) {
        const fields = fieldIndices.map((i) => this.fields[i]).filter(Boolean);
        return new _Schema(fields, this.metadata);
      }
      assign(...args) {
        const other = args[0] instanceof _Schema ? args[0] : Array.isArray(args[0]) ? new _Schema(args[0]) : new _Schema(args);
        const curFields = [...this.fields];
        const metadata = mergeMaps(mergeMaps(/* @__PURE__ */ new Map(), this.metadata), other.metadata);
        const newFields = other.fields.filter((f2) => {
          const i = curFields.findIndex((f) => f.name === f2.name);
          return ~i ? (curFields[i] = f2.clone({
            metadata: mergeMaps(mergeMaps(/* @__PURE__ */ new Map(), curFields[i].metadata), f2.metadata)
          })) && false : true;
        });
        const newDictionaries = generateDictionaryMap(newFields, /* @__PURE__ */ new Map());
        return new _Schema([...curFields, ...newFields], metadata, new Map([...this.dictionaries, ...newDictionaries]));
      }
    };
    exports2.Schema = Schema;
    Schema.prototype.fields = null;
    Schema.prototype.metadata = null;
    Schema.prototype.dictionaries = null;
    var Field = class _Field {
      /** @nocollapse */
      static new(...args) {
        let [name, type, nullable, metadata] = args;
        if (args[0] && typeof args[0] === "object") {
          ({ name } = args[0]);
          type === void 0 && (type = args[0].type);
          nullable === void 0 && (nullable = args[0].nullable);
          metadata === void 0 && (metadata = args[0].metadata);
        }
        return new _Field(`${name}`, type, nullable, metadata);
      }
      constructor(name, type, nullable = false, metadata) {
        this.name = name;
        this.type = type;
        this.nullable = nullable;
        this.metadata = metadata || /* @__PURE__ */ new Map();
      }
      get typeId() {
        return this.type.typeId;
      }
      get [Symbol.toStringTag]() {
        return "Field";
      }
      toString() {
        return `${this.name}: ${this.type}`;
      }
      clone(...args) {
        let [name, type, nullable, metadata] = args;
        !args[0] || typeof args[0] !== "object" ? [name = this.name, type = this.type, nullable = this.nullable, metadata = this.metadata] = args : { name = this.name, type = this.type, nullable = this.nullable, metadata = this.metadata } = args[0];
        return _Field.new(name, type, nullable, metadata);
      }
    };
    exports2.Field = Field;
    Field.prototype.type = null;
    Field.prototype.name = null;
    Field.prototype.nullable = null;
    Field.prototype.metadata = null;
    function mergeMaps(m1, m2) {
      return new Map([...m1 || /* @__PURE__ */ new Map(), ...m2 || /* @__PURE__ */ new Map()]);
    }
    function generateDictionaryMap(fields, dictionaries = /* @__PURE__ */ new Map()) {
      for (let i = -1, n = fields.length; ++i < n; ) {
        const field = fields[i];
        const type = field.type;
        if (type_js_1.DataType.isDictionary(type)) {
          if (!dictionaries.has(type.id)) {
            dictionaries.set(type.id, type.dictionary);
          } else if (dictionaries.get(type.id) !== type.dictionary) {
            throw new Error(`Cannot create Schema containing two different dictionaries with the same Id`);
          }
        }
        if (type.children && type.children.length > 0) {
          generateDictionaryMap(type.children, dictionaries);
        }
      }
      return dictionaries;
    }
  }
});

// node_modules/apache-arrow/ipc/metadata/file.js
var require_file = __commonJS({
  "node_modules/apache-arrow/ipc/metadata/file.js"(exports2) {
    "use strict";
    Object.defineProperty(exports2, "__esModule", { value: true });
    exports2.FileBlock = exports2.Footer = void 0;
    var block_js_1 = require_block();
    var footer_js_1 = require_footer();
    var flatbuffers = (init_flatbuffers(), __toCommonJS(flatbuffers_exports));
    var Builder2 = flatbuffers.Builder;
    var ByteBuffer2 = flatbuffers.ByteBuffer;
    var schema_js_1 = require_schema2();
    var enum_js_1 = require_enum();
    var buffer_js_1 = require_buffer();
    var bigint_js_1 = require_bigint();
    var Footer_ = class {
      /** @nocollapse */
      static decode(buf) {
        buf = new ByteBuffer2((0, buffer_js_1.toUint8Array)(buf));
        const footer = footer_js_1.Footer.getRootAsFooter(buf);
        const schema = schema_js_1.Schema.decode(footer.schema(), /* @__PURE__ */ new Map(), footer.version());
        return new OffHeapFooter(schema, footer);
      }
      /** @nocollapse */
      static encode(footer) {
        const b = new Builder2();
        const schemaOffset = schema_js_1.Schema.encode(b, footer.schema);
        footer_js_1.Footer.startRecordBatchesVector(b, footer.numRecordBatches);
        for (const rb of [...footer.recordBatches()].slice().reverse()) {
          FileBlock.encode(b, rb);
        }
        const recordBatchesOffset = b.endVector();
        footer_js_1.Footer.startDictionariesVector(b, footer.numDictionaries);
        for (const db2 of [...footer.dictionaryBatches()].slice().reverse()) {
          FileBlock.encode(b, db2);
        }
        const dictionaryBatchesOffset = b.endVector();
        footer_js_1.Footer.startFooter(b);
        footer_js_1.Footer.addSchema(b, schemaOffset);
        footer_js_1.Footer.addVersion(b, enum_js_1.MetadataVersion.V5);
        footer_js_1.Footer.addRecordBatches(b, recordBatchesOffset);
        footer_js_1.Footer.addDictionaries(b, dictionaryBatchesOffset);
        footer_js_1.Footer.finishFooterBuffer(b, footer_js_1.Footer.endFooter(b));
        return b.asUint8Array();
      }
      get numRecordBatches() {
        return this._recordBatches.length;
      }
      get numDictionaries() {
        return this._dictionaryBatches.length;
      }
      constructor(schema, version = enum_js_1.MetadataVersion.V5, recordBatches, dictionaryBatches) {
        this.schema = schema;
        this.version = version;
        recordBatches && (this._recordBatches = recordBatches);
        dictionaryBatches && (this._dictionaryBatches = dictionaryBatches);
      }
      *recordBatches() {
        for (let block, i = -1, n = this.numRecordBatches; ++i < n; ) {
          if (block = this.getRecordBatch(i)) {
            yield block;
          }
        }
      }
      *dictionaryBatches() {
        for (let block, i = -1, n = this.numDictionaries; ++i < n; ) {
          if (block = this.getDictionaryBatch(i)) {
            yield block;
          }
        }
      }
      getRecordBatch(index) {
        return index >= 0 && index < this.numRecordBatches && this._recordBatches[index] || null;
      }
      getDictionaryBatch(index) {
        return index >= 0 && index < this.numDictionaries && this._dictionaryBatches[index] || null;
      }
    };
    exports2.Footer = Footer_;
    var OffHeapFooter = class extends Footer_ {
      get numRecordBatches() {
        return this._footer.recordBatchesLength();
      }
      get numDictionaries() {
        return this._footer.dictionariesLength();
      }
      constructor(schema, _footer) {
        super(schema, _footer.version());
        this._footer = _footer;
      }
      getRecordBatch(index) {
        if (index >= 0 && index < this.numRecordBatches) {
          const fileBlock = this._footer.recordBatches(index);
          if (fileBlock) {
            return FileBlock.decode(fileBlock);
          }
        }
        return null;
      }
      getDictionaryBatch(index) {
        if (index >= 0 && index < this.numDictionaries) {
          const fileBlock = this._footer.dictionaries(index);
          if (fileBlock) {
            return FileBlock.decode(fileBlock);
          }
        }
        return null;
      }
    };
    var FileBlock = class _FileBlock {
      /** @nocollapse */
      static decode(block) {
        return new _FileBlock(block.metaDataLength(), block.bodyLength(), block.offset());
      }
      /** @nocollapse */
      static encode(b, fileBlock) {
        const { metaDataLength } = fileBlock;
        const offset = BigInt(fileBlock.offset);
        const bodyLength = BigInt(fileBlock.bodyLength);
        return block_js_1.Block.createBlock(b, offset, metaDataLength, bodyLength);
      }
      constructor(metaDataLength, bodyLength, offset) {
        this.metaDataLength = metaDataLength;
        this.offset = (0, bigint_js_1.bigIntToNumber)(offset);
        this.bodyLength = (0, bigint_js_1.bigIntToNumber)(bodyLength);
      }
    };
    exports2.FileBlock = FileBlock;
  }
});

// node_modules/apache-arrow/io/interfaces.js
var require_interfaces = __commonJS({
  "node_modules/apache-arrow/io/interfaces.js"(exports2) {
    "use strict";
    Object.defineProperty(exports2, "__esModule", { value: true });
    exports2.AsyncQueue = exports2.ReadableInterop = exports2.ArrowJSON = exports2.ITERATOR_DONE = void 0;
    var tslib_1 = (init_tslib_es6(), __toCommonJS(tslib_es6_exports));
    var adapters_js_1 = require_adapters();
    exports2.ITERATOR_DONE = Object.freeze({ done: true, value: void 0 });
    var ArrowJSON = class {
      constructor(_json) {
        this._json = _json;
      }
      get schema() {
        return this._json["schema"];
      }
      get batches() {
        return this._json["batches"] || [];
      }
      get dictionaries() {
        return this._json["dictionaries"] || [];
      }
    };
    exports2.ArrowJSON = ArrowJSON;
    var ReadableInterop = class {
      tee() {
        return this._getDOMStream().tee();
      }
      pipe(writable, options) {
        return this._getNodeStream().pipe(writable, options);
      }
      pipeTo(writable, options) {
        return this._getDOMStream().pipeTo(writable, options);
      }
      pipeThrough(duplex, options) {
        return this._getDOMStream().pipeThrough(duplex, options);
      }
      _getDOMStream() {
        return this._DOMStream || (this._DOMStream = this.toDOMStream());
      }
      _getNodeStream() {
        return this._nodeStream || (this._nodeStream = this.toNodeStream());
      }
    };
    exports2.ReadableInterop = ReadableInterop;
    var AsyncQueue = class extends ReadableInterop {
      constructor() {
        super();
        this._values = [];
        this.resolvers = [];
        this._closedPromise = new Promise((r) => this._closedPromiseResolve = r);
      }
      get closed() {
        return this._closedPromise;
      }
      cancel(reason) {
        return tslib_1.__awaiter(this, void 0, void 0, function* () {
          yield this.return(reason);
        });
      }
      write(value) {
        if (this._ensureOpen()) {
          this.resolvers.length <= 0 ? this._values.push(value) : this.resolvers.shift().resolve({ done: false, value });
        }
      }
      abort(value) {
        if (this._closedPromiseResolve) {
          this.resolvers.length <= 0 ? this._error = { error: value } : this.resolvers.shift().reject({ done: true, value });
        }
      }
      close() {
        if (this._closedPromiseResolve) {
          const { resolvers } = this;
          while (resolvers.length > 0) {
            resolvers.shift().resolve(exports2.ITERATOR_DONE);
          }
          this._closedPromiseResolve();
          this._closedPromiseResolve = void 0;
        }
      }
      [Symbol.asyncIterator]() {
        return this;
      }
      toDOMStream(options) {
        return adapters_js_1.default.toDOMStream(this._closedPromiseResolve || this._error ? this : this._values, options);
      }
      toNodeStream(options) {
        return adapters_js_1.default.toNodeStream(this._closedPromiseResolve || this._error ? this : this._values, options);
      }
      throw(_) {
        return tslib_1.__awaiter(this, void 0, void 0, function* () {
          yield this.abort(_);
          return exports2.ITERATOR_DONE;
        });
      }
      return(_) {
        return tslib_1.__awaiter(this, void 0, void 0, function* () {
          yield this.close();
          return exports2.ITERATOR_DONE;
        });
      }
      read(size) {
        return tslib_1.__awaiter(this, void 0, void 0, function* () {
          return (yield this.next(size, "read")).value;
        });
      }
      peek(size) {
        return tslib_1.__awaiter(this, void 0, void 0, function* () {
          return (yield this.next(size, "peek")).value;
        });
      }
      next(..._args) {
        if (this._values.length > 0) {
          return Promise.resolve({ done: false, value: this._values.shift() });
        } else if (this._error) {
          return Promise.reject({ done: true, value: this._error.error });
        } else if (!this._closedPromiseResolve) {
          return Promise.resolve(exports2.ITERATOR_DONE);
        } else {
          return new Promise((resolve, reject) => {
            this.resolvers.push({ resolve, reject });
          });
        }
      }
      _ensureOpen() {
        if (this._closedPromiseResolve) {
          return true;
        }
        throw new Error(`AsyncQueue is closed`);
      }
    };
    exports2.AsyncQueue = AsyncQueue;
  }
});

// node_modules/apache-arrow/io/stream.js
var require_stream = __commonJS({
  "node_modules/apache-arrow/io/stream.js"(exports2) {
    "use strict";
    Object.defineProperty(exports2, "__esModule", { value: true });
    exports2.AsyncByteStream = exports2.ByteStream = exports2.AsyncByteQueue = void 0;
    var tslib_1 = (init_tslib_es6(), __toCommonJS(tslib_es6_exports));
    var adapters_js_1 = require_adapters();
    var utf8_js_1 = require_utf8();
    var interfaces_js_1 = require_interfaces();
    var buffer_js_1 = require_buffer();
    var compat_js_1 = require_compat();
    var AsyncByteQueue = class extends interfaces_js_1.AsyncQueue {
      write(value) {
        if ((value = (0, buffer_js_1.toUint8Array)(value)).byteLength > 0) {
          return super.write(value);
        }
      }
      toString(sync = false) {
        return sync ? (0, utf8_js_1.decodeUtf8)(this.toUint8Array(true)) : this.toUint8Array(false).then(utf8_js_1.decodeUtf8);
      }
      toUint8Array(sync = false) {
        return sync ? (0, buffer_js_1.joinUint8Arrays)(this._values)[0] : (() => tslib_1.__awaiter(this, void 0, void 0, function* () {
          var _a, e_1, _b, _c;
          const buffers = [];
          let byteLength = 0;
          try {
            for (var _d = true, _e = tslib_1.__asyncValues(this), _f; _f = yield _e.next(), _a = _f.done, !_a; _d = true) {
              _c = _f.value;
              _d = false;
              const chunk = _c;
              buffers.push(chunk);
              byteLength += chunk.byteLength;
            }
          } catch (e_1_1) {
            e_1 = { error: e_1_1 };
          } finally {
            try {
              if (!_d && !_a && (_b = _e.return)) yield _b.call(_e);
            } finally {
              if (e_1) throw e_1.error;
            }
          }
          return (0, buffer_js_1.joinUint8Arrays)(buffers, byteLength)[0];
        }))();
      }
    };
    exports2.AsyncByteQueue = AsyncByteQueue;
    var ByteStream = class {
      constructor(source) {
        if (source) {
          this.source = new ByteStreamSource(adapters_js_1.default.fromIterable(source));
        }
      }
      [Symbol.iterator]() {
        return this;
      }
      next(value) {
        return this.source.next(value);
      }
      throw(value) {
        return this.source.throw(value);
      }
      return(value) {
        return this.source.return(value);
      }
      peek(size) {
        return this.source.peek(size);
      }
      read(size) {
        return this.source.read(size);
      }
    };
    exports2.ByteStream = ByteStream;
    var AsyncByteStream = class _AsyncByteStream {
      constructor(source) {
        if (source instanceof _AsyncByteStream) {
          this.source = source.source;
        } else if (source instanceof AsyncByteQueue) {
          this.source = new AsyncByteStreamSource(adapters_js_1.default.fromAsyncIterable(source));
        } else if ((0, compat_js_1.isReadableNodeStream)(source)) {
          this.source = new AsyncByteStreamSource(adapters_js_1.default.fromNodeStream(source));
        } else if ((0, compat_js_1.isReadableDOMStream)(source)) {
          this.source = new AsyncByteStreamSource(adapters_js_1.default.fromDOMStream(source));
        } else if ((0, compat_js_1.isFetchResponse)(source)) {
          this.source = new AsyncByteStreamSource(adapters_js_1.default.fromDOMStream(source.body));
        } else if ((0, compat_js_1.isIterable)(source)) {
          this.source = new AsyncByteStreamSource(adapters_js_1.default.fromIterable(source));
        } else if ((0, compat_js_1.isPromise)(source)) {
          this.source = new AsyncByteStreamSource(adapters_js_1.default.fromAsyncIterable(source));
        } else if ((0, compat_js_1.isAsyncIterable)(source)) {
          this.source = new AsyncByteStreamSource(adapters_js_1.default.fromAsyncIterable(source));
        }
      }
      [Symbol.asyncIterator]() {
        return this;
      }
      next(value) {
        return this.source.next(value);
      }
      throw(value) {
        return this.source.throw(value);
      }
      return(value) {
        return this.source.return(value);
      }
      get closed() {
        return this.source.closed;
      }
      cancel(reason) {
        return this.source.cancel(reason);
      }
      peek(size) {
        return this.source.peek(size);
      }
      read(size) {
        return this.source.read(size);
      }
    };
    exports2.AsyncByteStream = AsyncByteStream;
    var ByteStreamSource = class {
      constructor(source) {
        this.source = source;
      }
      cancel(reason) {
        this.return(reason);
      }
      peek(size) {
        return this.next(size, "peek").value;
      }
      read(size) {
        return this.next(size, "read").value;
      }
      next(size, cmd = "read") {
        return this.source.next({ cmd, size });
      }
      throw(value) {
        return Object.create(this.source.throw && this.source.throw(value) || interfaces_js_1.ITERATOR_DONE);
      }
      return(value) {
        return Object.create(this.source.return && this.source.return(value) || interfaces_js_1.ITERATOR_DONE);
      }
    };
    var AsyncByteStreamSource = class {
      constructor(source) {
        this.source = source;
        this._closedPromise = new Promise((r) => this._closedPromiseResolve = r);
      }
      cancel(reason) {
        return tslib_1.__awaiter(this, void 0, void 0, function* () {
          yield this.return(reason);
        });
      }
      get closed() {
        return this._closedPromise;
      }
      read(size) {
        return tslib_1.__awaiter(this, void 0, void 0, function* () {
          return (yield this.next(size, "read")).value;
        });
      }
      peek(size) {
        return tslib_1.__awaiter(this, void 0, void 0, function* () {
          return (yield this.next(size, "peek")).value;
        });
      }
      next(size_1) {
        return tslib_1.__awaiter(this, arguments, void 0, function* (size, cmd = "read") {
          return yield this.source.next({ cmd, size });
        });
      }
      throw(value) {
        return tslib_1.__awaiter(this, void 0, void 0, function* () {
          const result = this.source.throw && (yield this.source.throw(value)) || interfaces_js_1.ITERATOR_DONE;
          this._closedPromiseResolve && this._closedPromiseResolve();
          this._closedPromiseResolve = void 0;
          return Object.create(result);
        });
      }
      return(value) {
        return tslib_1.__awaiter(this, void 0, void 0, function* () {
          const result = this.source.return && (yield this.source.return(value)) || interfaces_js_1.ITERATOR_DONE;
          this._closedPromiseResolve && this._closedPromiseResolve();
          this._closedPromiseResolve = void 0;
          return Object.create(result);
        });
      }
    };
  }
});

// node_modules/apache-arrow/io/file.js
var require_file2 = __commonJS({
  "node_modules/apache-arrow/io/file.js"(exports2) {
    "use strict";
    Object.defineProperty(exports2, "__esModule", { value: true });
    exports2.AsyncRandomAccessFile = exports2.RandomAccessFile = void 0;
    var tslib_1 = (init_tslib_es6(), __toCommonJS(tslib_es6_exports));
    var stream_js_1 = require_stream();
    var buffer_js_1 = require_buffer();
    var RandomAccessFile = class extends stream_js_1.ByteStream {
      constructor(buffer, byteLength) {
        super();
        this.position = 0;
        this.buffer = (0, buffer_js_1.toUint8Array)(buffer);
        this.size = byteLength === void 0 ? this.buffer.byteLength : byteLength;
      }
      readInt32(position) {
        const { buffer, byteOffset } = this.readAt(position, 4);
        return new DataView(buffer, byteOffset).getInt32(0, true);
      }
      seek(position) {
        this.position = Math.min(position, this.size);
        return position < this.size;
      }
      read(nBytes) {
        const { buffer, size, position } = this;
        if (buffer && position < size) {
          if (typeof nBytes !== "number") {
            nBytes = Number.POSITIVE_INFINITY;
          }
          this.position = Math.min(size, position + Math.min(size - position, nBytes));
          return buffer.subarray(position, this.position);
        }
        return null;
      }
      readAt(position, nBytes) {
        const buf = this.buffer;
        const end = Math.min(this.size, position + nBytes);
        return buf ? buf.subarray(position, end) : new Uint8Array(nBytes);
      }
      close() {
        this.buffer && (this.buffer = null);
      }
      throw(value) {
        this.close();
        return { done: true, value };
      }
      return(value) {
        this.close();
        return { done: true, value };
      }
    };
    exports2.RandomAccessFile = RandomAccessFile;
    var AsyncRandomAccessFile = class extends stream_js_1.AsyncByteStream {
      constructor(file, byteLength) {
        super();
        this.position = 0;
        this._handle = file;
        if (typeof byteLength === "number") {
          this.size = byteLength;
        } else {
          this._pending = (() => tslib_1.__awaiter(this, void 0, void 0, function* () {
            this.size = (yield file.stat()).size;
            delete this._pending;
          }))();
        }
      }
      readInt32(position) {
        return tslib_1.__awaiter(this, void 0, void 0, function* () {
          const { buffer, byteOffset } = yield this.readAt(position, 4);
          return new DataView(buffer, byteOffset).getInt32(0, true);
        });
      }
      seek(position) {
        return tslib_1.__awaiter(this, void 0, void 0, function* () {
          this._pending && (yield this._pending);
          this.position = Math.min(position, this.size);
          return position < this.size;
        });
      }
      read(nBytes) {
        return tslib_1.__awaiter(this, void 0, void 0, function* () {
          this._pending && (yield this._pending);
          const { _handle: file, size, position } = this;
          if (file && position < size) {
            if (typeof nBytes !== "number") {
              nBytes = Number.POSITIVE_INFINITY;
            }
            let pos = position, offset = 0, bytesRead = 0;
            const end = Math.min(size, pos + Math.min(size - pos, nBytes));
            const buffer = new Uint8Array(Math.max(0, (this.position = end) - pos));
            while ((pos += bytesRead) < end && (offset += bytesRead) < buffer.byteLength) {
              ({ bytesRead } = yield file.read(buffer, offset, buffer.byteLength - offset, pos));
            }
            return buffer;
          }
          return null;
        });
      }
      readAt(position, nBytes) {
        return tslib_1.__awaiter(this, void 0, void 0, function* () {
          this._pending && (yield this._pending);
          const { _handle: file, size } = this;
          if (file && position + nBytes < size) {
            const end = Math.min(size, position + nBytes);
            const buffer = new Uint8Array(end - position);
            return (yield file.read(buffer, 0, nBytes, position)).buffer;
          }
          return new Uint8Array(nBytes);
        });
      }
      close() {
        return tslib_1.__awaiter(this, void 0, void 0, function* () {
          const f = this._handle;
          this._handle = null;
          f && (yield f.close());
        });
      }
      throw(value) {
        return tslib_1.__awaiter(this, void 0, void 0, function* () {
          yield this.close();
          return { done: true, value };
        });
      }
      return(value) {
        return tslib_1.__awaiter(this, void 0, void 0, function* () {
          yield this.close();
          return { done: true, value };
        });
      }
    };
    exports2.AsyncRandomAccessFile = AsyncRandomAccessFile;
  }
});

// node_modules/apache-arrow/util/int.js
var require_int2 = __commonJS({
  "node_modules/apache-arrow/util/int.js"(exports2) {
    "use strict";
    Object.defineProperty(exports2, "__esModule", { value: true });
    exports2.Int128 = exports2.Int64 = exports2.Uint64 = exports2.BaseInt64 = void 0;
    var carryBit16 = 1 << 16;
    function intAsHex(value) {
      if (value < 0) {
        value = 4294967295 + value + 1;
      }
      return `0x${value.toString(16)}`;
    }
    var kInt32DecimalDigits = 8;
    var kPowersOfTen = [
      1,
      10,
      100,
      1e3,
      1e4,
      1e5,
      1e6,
      1e7,
      1e8
    ];
    var BaseInt64 = class {
      constructor(buffer) {
        this.buffer = buffer;
      }
      high() {
        return this.buffer[1];
      }
      low() {
        return this.buffer[0];
      }
      _times(other) {
        const L = new Uint32Array([
          this.buffer[1] >>> 16,
          this.buffer[1] & 65535,
          this.buffer[0] >>> 16,
          this.buffer[0] & 65535
        ]);
        const R = new Uint32Array([
          other.buffer[1] >>> 16,
          other.buffer[1] & 65535,
          other.buffer[0] >>> 16,
          other.buffer[0] & 65535
        ]);
        let product = L[3] * R[3];
        this.buffer[0] = product & 65535;
        let sum = product >>> 16;
        product = L[2] * R[3];
        sum += product;
        product = L[3] * R[2] >>> 0;
        sum += product;
        this.buffer[0] += sum << 16;
        this.buffer[1] = sum >>> 0 < product ? carryBit16 : 0;
        this.buffer[1] += sum >>> 16;
        this.buffer[1] += L[1] * R[3] + L[2] * R[2] + L[3] * R[1];
        this.buffer[1] += L[0] * R[3] + L[1] * R[2] + L[2] * R[1] + L[3] * R[0] << 16;
        return this;
      }
      _plus(other) {
        const sum = this.buffer[0] + other.buffer[0] >>> 0;
        this.buffer[1] += other.buffer[1];
        if (sum < this.buffer[0] >>> 0) {
          ++this.buffer[1];
        }
        this.buffer[0] = sum;
      }
      lessThan(other) {
        return this.buffer[1] < other.buffer[1] || this.buffer[1] === other.buffer[1] && this.buffer[0] < other.buffer[0];
      }
      equals(other) {
        return this.buffer[1] === other.buffer[1] && this.buffer[0] == other.buffer[0];
      }
      greaterThan(other) {
        return other.lessThan(this);
      }
      hex() {
        return `${intAsHex(this.buffer[1])} ${intAsHex(this.buffer[0])}`;
      }
    };
    exports2.BaseInt64 = BaseInt64;
    var Uint64 = class _Uint64 extends BaseInt64 {
      times(other) {
        this._times(other);
        return this;
      }
      plus(other) {
        this._plus(other);
        return this;
      }
      /** @nocollapse */
      static from(val, out_buffer = new Uint32Array(2)) {
        return _Uint64.fromString(typeof val === "string" ? val : val.toString(), out_buffer);
      }
      /** @nocollapse */
      static fromNumber(num, out_buffer = new Uint32Array(2)) {
        return _Uint64.fromString(num.toString(), out_buffer);
      }
      /** @nocollapse */
      static fromString(str, out_buffer = new Uint32Array(2)) {
        const length = str.length;
        const out = new _Uint64(out_buffer);
        for (let posn = 0; posn < length; ) {
          const group = kInt32DecimalDigits < length - posn ? kInt32DecimalDigits : length - posn;
          const chunk = new _Uint64(new Uint32Array([Number.parseInt(str.slice(posn, posn + group), 10), 0]));
          const multiple = new _Uint64(new Uint32Array([kPowersOfTen[group], 0]));
          out.times(multiple);
          out.plus(chunk);
          posn += group;
        }
        return out;
      }
      /** @nocollapse */
      static convertArray(values) {
        const data = new Uint32Array(values.length * 2);
        for (let i = -1, n = values.length; ++i < n; ) {
          _Uint64.from(values[i], new Uint32Array(data.buffer, data.byteOffset + 2 * i * 4, 2));
        }
        return data;
      }
      /** @nocollapse */
      static multiply(left, right) {
        const rtrn = new _Uint64(new Uint32Array(left.buffer));
        return rtrn.times(right);
      }
      /** @nocollapse */
      static add(left, right) {
        const rtrn = new _Uint64(new Uint32Array(left.buffer));
        return rtrn.plus(right);
      }
    };
    exports2.Uint64 = Uint64;
    var Int64 = class _Int64 extends BaseInt64 {
      negate() {
        this.buffer[0] = ~this.buffer[0] + 1;
        this.buffer[1] = ~this.buffer[1];
        if (this.buffer[0] == 0) {
          ++this.buffer[1];
        }
        return this;
      }
      times(other) {
        this._times(other);
        return this;
      }
      plus(other) {
        this._plus(other);
        return this;
      }
      lessThan(other) {
        const this_high = this.buffer[1] << 0;
        const other_high = other.buffer[1] << 0;
        return this_high < other_high || this_high === other_high && this.buffer[0] < other.buffer[0];
      }
      /** @nocollapse */
      static from(val, out_buffer = new Uint32Array(2)) {
        return _Int64.fromString(typeof val === "string" ? val : val.toString(), out_buffer);
      }
      /** @nocollapse */
      static fromNumber(num, out_buffer = new Uint32Array(2)) {
        return _Int64.fromString(num.toString(), out_buffer);
      }
      /** @nocollapse */
      static fromString(str, out_buffer = new Uint32Array(2)) {
        const negate = str.startsWith("-");
        const length = str.length;
        const out = new _Int64(out_buffer);
        for (let posn = negate ? 1 : 0; posn < length; ) {
          const group = kInt32DecimalDigits < length - posn ? kInt32DecimalDigits : length - posn;
          const chunk = new _Int64(new Uint32Array([Number.parseInt(str.slice(posn, posn + group), 10), 0]));
          const multiple = new _Int64(new Uint32Array([kPowersOfTen[group], 0]));
          out.times(multiple);
          out.plus(chunk);
          posn += group;
        }
        return negate ? out.negate() : out;
      }
      /** @nocollapse */
      static convertArray(values) {
        const data = new Uint32Array(values.length * 2);
        for (let i = -1, n = values.length; ++i < n; ) {
          _Int64.from(values[i], new Uint32Array(data.buffer, data.byteOffset + 2 * i * 4, 2));
        }
        return data;
      }
      /** @nocollapse */
      static multiply(left, right) {
        const rtrn = new _Int64(new Uint32Array(left.buffer));
        return rtrn.times(right);
      }
      /** @nocollapse */
      static add(left, right) {
        const rtrn = new _Int64(new Uint32Array(left.buffer));
        return rtrn.plus(right);
      }
    };
    exports2.Int64 = Int64;
    var Int128 = class _Int128 {
      constructor(buffer) {
        this.buffer = buffer;
      }
      high() {
        return new Int64(new Uint32Array(this.buffer.buffer, this.buffer.byteOffset + 8, 2));
      }
      low() {
        return new Int64(new Uint32Array(this.buffer.buffer, this.buffer.byteOffset, 2));
      }
      negate() {
        this.buffer[0] = ~this.buffer[0] + 1;
        this.buffer[1] = ~this.buffer[1];
        this.buffer[2] = ~this.buffer[2];
        this.buffer[3] = ~this.buffer[3];
        if (this.buffer[0] == 0) {
          ++this.buffer[1];
        }
        if (this.buffer[1] == 0) {
          ++this.buffer[2];
        }
        if (this.buffer[2] == 0) {
          ++this.buffer[3];
        }
        return this;
      }
      times(other) {
        const L0 = new Uint64(new Uint32Array([this.buffer[3], 0]));
        const L1 = new Uint64(new Uint32Array([this.buffer[2], 0]));
        const L2 = new Uint64(new Uint32Array([this.buffer[1], 0]));
        const L3 = new Uint64(new Uint32Array([this.buffer[0], 0]));
        const R0 = new Uint64(new Uint32Array([other.buffer[3], 0]));
        const R1 = new Uint64(new Uint32Array([other.buffer[2], 0]));
        const R2 = new Uint64(new Uint32Array([other.buffer[1], 0]));
        const R3 = new Uint64(new Uint32Array([other.buffer[0], 0]));
        let product = Uint64.multiply(L3, R3);
        this.buffer[0] = product.low();
        const sum = new Uint64(new Uint32Array([product.high(), 0]));
        product = Uint64.multiply(L2, R3);
        sum.plus(product);
        product = Uint64.multiply(L3, R2);
        sum.plus(product);
        this.buffer[1] = sum.low();
        this.buffer[3] = sum.lessThan(product) ? 1 : 0;
        this.buffer[2] = sum.high();
        const high = new Uint64(new Uint32Array(this.buffer.buffer, this.buffer.byteOffset + 8, 2));
        high.plus(Uint64.multiply(L1, R3)).plus(Uint64.multiply(L2, R2)).plus(Uint64.multiply(L3, R1));
        this.buffer[3] += Uint64.multiply(L0, R3).plus(Uint64.multiply(L1, R2)).plus(Uint64.multiply(L2, R1)).plus(Uint64.multiply(L3, R0)).low();
        return this;
      }
      plus(other) {
        const sums = new Uint32Array(4);
        sums[3] = this.buffer[3] + other.buffer[3] >>> 0;
        sums[2] = this.buffer[2] + other.buffer[2] >>> 0;
        sums[1] = this.buffer[1] + other.buffer[1] >>> 0;
        sums[0] = this.buffer[0] + other.buffer[0] >>> 0;
        if (sums[0] < this.buffer[0] >>> 0) {
          ++sums[1];
        }
        if (sums[1] < this.buffer[1] >>> 0) {
          ++sums[2];
        }
        if (sums[2] < this.buffer[2] >>> 0) {
          ++sums[3];
        }
        this.buffer[3] = sums[3];
        this.buffer[2] = sums[2];
        this.buffer[1] = sums[1];
        this.buffer[0] = sums[0];
        return this;
      }
      hex() {
        return `${intAsHex(this.buffer[3])} ${intAsHex(this.buffer[2])} ${intAsHex(this.buffer[1])} ${intAsHex(this.buffer[0])}`;
      }
      /** @nocollapse */
      static multiply(left, right) {
        const rtrn = new _Int128(new Uint32Array(left.buffer));
        return rtrn.times(right);
      }
      /** @nocollapse */
      static add(left, right) {
        const rtrn = new _Int128(new Uint32Array(left.buffer));
        return rtrn.plus(right);
      }
      /** @nocollapse */
      static from(val, out_buffer = new Uint32Array(4)) {
        return _Int128.fromString(typeof val === "string" ? val : val.toString(), out_buffer);
      }
      /** @nocollapse */
      static fromNumber(num, out_buffer = new Uint32Array(4)) {
        return _Int128.fromString(num.toString(), out_buffer);
      }
      /** @nocollapse */
      static fromString(str, out_buffer = new Uint32Array(4)) {
        const negate = str.startsWith("-");
        const length = str.length;
        const out = new _Int128(out_buffer);
        for (let posn = negate ? 1 : 0; posn < length; ) {
          const group = kInt32DecimalDigits < length - posn ? kInt32DecimalDigits : length - posn;
          const chunk = new _Int128(new Uint32Array([Number.parseInt(str.slice(posn, posn + group), 10), 0, 0, 0]));
          const multiple = new _Int128(new Uint32Array([kPowersOfTen[group], 0, 0, 0]));
          out.times(multiple);
          out.plus(chunk);
          posn += group;
        }
        return negate ? out.negate() : out;
      }
      /** @nocollapse */
      static convertArray(values) {
        const data = new Uint32Array(values.length * 4);
        for (let i = -1, n = values.length; ++i < n; ) {
          _Int128.from(values[i], new Uint32Array(data.buffer, data.byteOffset + 4 * 4 * i, 4));
        }
        return data;
      }
    };
    exports2.Int128 = Int128;
  }
});

// node_modules/apache-arrow/visitor/vectorloader.js
var require_vectorloader = __commonJS({
  "node_modules/apache-arrow/visitor/vectorloader.js"(exports2) {
    "use strict";
    Object.defineProperty(exports2, "__esModule", { value: true });
    exports2.JSONVectorLoader = exports2.VectorLoader = void 0;
    var data_js_1 = require_data();
    var schema_js_1 = require_schema2();
    var type_js_1 = require_type2();
    var visitor_js_1 = require_visitor();
    var bit_js_1 = require_bit();
    var utf8_js_1 = require_utf8();
    var int_js_1 = require_int2();
    var enum_js_1 = require_enum();
    var buffer_js_1 = require_buffer();
    var VectorLoader = class extends visitor_js_1.Visitor {
      constructor(bytes, nodes, buffers, dictionaries, metadataVersion = enum_js_1.MetadataVersion.V5) {
        super();
        this.nodesIndex = -1;
        this.buffersIndex = -1;
        this.bytes = bytes;
        this.nodes = nodes;
        this.buffers = buffers;
        this.dictionaries = dictionaries;
        this.metadataVersion = metadataVersion;
      }
      visit(node) {
        return super.visit(node instanceof schema_js_1.Field ? node.type : node);
      }
      visitNull(type, { length } = this.nextFieldNode()) {
        return (0, data_js_1.makeData)({ type, length });
      }
      visitBool(type, { length, nullCount } = this.nextFieldNode()) {
        return (0, data_js_1.makeData)({ type, length, nullCount, nullBitmap: this.readNullBitmap(type, nullCount), data: this.readData(type) });
      }
      visitInt(type, { length, nullCount } = this.nextFieldNode()) {
        return (0, data_js_1.makeData)({ type, length, nullCount, nullBitmap: this.readNullBitmap(type, nullCount), data: this.readData(type) });
      }
      visitFloat(type, { length, nullCount } = this.nextFieldNode()) {
        return (0, data_js_1.makeData)({ type, length, nullCount, nullBitmap: this.readNullBitmap(type, nullCount), data: this.readData(type) });
      }
      visitUtf8(type, { length, nullCount } = this.nextFieldNode()) {
        return (0, data_js_1.makeData)({ type, length, nullCount, nullBitmap: this.readNullBitmap(type, nullCount), valueOffsets: this.readOffsets(type), data: this.readData(type) });
      }
      visitLargeUtf8(type, { length, nullCount } = this.nextFieldNode()) {
        return (0, data_js_1.makeData)({ type, length, nullCount, nullBitmap: this.readNullBitmap(type, nullCount), valueOffsets: this.readOffsets(type), data: this.readData(type) });
      }
      visitBinary(type, { length, nullCount } = this.nextFieldNode()) {
        return (0, data_js_1.makeData)({ type, length, nullCount, nullBitmap: this.readNullBitmap(type, nullCount), valueOffsets: this.readOffsets(type), data: this.readData(type) });
      }
      visitLargeBinary(type, { length, nullCount } = this.nextFieldNode()) {
        return (0, data_js_1.makeData)({ type, length, nullCount, nullBitmap: this.readNullBitmap(type, nullCount), valueOffsets: this.readOffsets(type), data: this.readData(type) });
      }
      visitFixedSizeBinary(type, { length, nullCount } = this.nextFieldNode()) {
        return (0, data_js_1.makeData)({ type, length, nullCount, nullBitmap: this.readNullBitmap(type, nullCount), data: this.readData(type) });
      }
      visitDate(type, { length, nullCount } = this.nextFieldNode()) {
        return (0, data_js_1.makeData)({ type, length, nullCount, nullBitmap: this.readNullBitmap(type, nullCount), data: this.readData(type) });
      }
      visitTimestamp(type, { length, nullCount } = this.nextFieldNode()) {
        return (0, data_js_1.makeData)({ type, length, nullCount, nullBitmap: this.readNullBitmap(type, nullCount), data: this.readData(type) });
      }
      visitTime(type, { length, nullCount } = this.nextFieldNode()) {
        return (0, data_js_1.makeData)({ type, length, nullCount, nullBitmap: this.readNullBitmap(type, nullCount), data: this.readData(type) });
      }
      visitDecimal(type, { length, nullCount } = this.nextFieldNode()) {
        return (0, data_js_1.makeData)({ type, length, nullCount, nullBitmap: this.readNullBitmap(type, nullCount), data: this.readData(type) });
      }
      visitList(type, { length, nullCount } = this.nextFieldNode()) {
        return (0, data_js_1.makeData)({ type, length, nullCount, nullBitmap: this.readNullBitmap(type, nullCount), valueOffsets: this.readOffsets(type), "child": this.visit(type.children[0]) });
      }
      visitStruct(type, { length, nullCount } = this.nextFieldNode()) {
        return (0, data_js_1.makeData)({ type, length, nullCount, nullBitmap: this.readNullBitmap(type, nullCount), children: this.visitMany(type.children) });
      }
      visitUnion(type, { length, nullCount } = this.nextFieldNode()) {
        if (this.metadataVersion < enum_js_1.MetadataVersion.V5) {
          this.readNullBitmap(type, nullCount);
        }
        return type.mode === enum_js_1.UnionMode.Sparse ? this.visitSparseUnion(type, { length, nullCount }) : this.visitDenseUnion(type, { length, nullCount });
      }
      visitDenseUnion(type, { length, nullCount } = this.nextFieldNode()) {
        return (0, data_js_1.makeData)({ type, length, nullCount, typeIds: this.readTypeIds(type), valueOffsets: this.readOffsets(type), children: this.visitMany(type.children) });
      }
      visitSparseUnion(type, { length, nullCount } = this.nextFieldNode()) {
        return (0, data_js_1.makeData)({ type, length, nullCount, typeIds: this.readTypeIds(type), children: this.visitMany(type.children) });
      }
      visitDictionary(type, { length, nullCount } = this.nextFieldNode()) {
        return (0, data_js_1.makeData)({ type, length, nullCount, nullBitmap: this.readNullBitmap(type, nullCount), data: this.readData(type.indices), dictionary: this.readDictionary(type) });
      }
      visitInterval(type, { length, nullCount } = this.nextFieldNode()) {
        return (0, data_js_1.makeData)({ type, length, nullCount, nullBitmap: this.readNullBitmap(type, nullCount), data: this.readData(type) });
      }
      visitDuration(type, { length, nullCount } = this.nextFieldNode()) {
        return (0, data_js_1.makeData)({ type, length, nullCount, nullBitmap: this.readNullBitmap(type, nullCount), data: this.readData(type) });
      }
      visitFixedSizeList(type, { length, nullCount } = this.nextFieldNode()) {
        return (0, data_js_1.makeData)({ type, length, nullCount, nullBitmap: this.readNullBitmap(type, nullCount), "child": this.visit(type.children[0]) });
      }
      visitMap(type, { length, nullCount } = this.nextFieldNode()) {
        return (0, data_js_1.makeData)({ type, length, nullCount, nullBitmap: this.readNullBitmap(type, nullCount), valueOffsets: this.readOffsets(type), "child": this.visit(type.children[0]) });
      }
      nextFieldNode() {
        return this.nodes[++this.nodesIndex];
      }
      nextBufferRange() {
        return this.buffers[++this.buffersIndex];
      }
      readNullBitmap(type, nullCount, buffer = this.nextBufferRange()) {
        return nullCount > 0 && this.readData(type, buffer) || new Uint8Array(0);
      }
      readOffsets(type, buffer) {
        return this.readData(type, buffer);
      }
      readTypeIds(type, buffer) {
        return this.readData(type, buffer);
      }
      readData(_type, { length, offset } = this.nextBufferRange()) {
        return this.bytes.subarray(offset, offset + length);
      }
      readDictionary(type) {
        return this.dictionaries.get(type.id);
      }
    };
    exports2.VectorLoader = VectorLoader;
    var JSONVectorLoader = class extends VectorLoader {
      constructor(sources, nodes, buffers, dictionaries, metadataVersion) {
        super(new Uint8Array(0), nodes, buffers, dictionaries, metadataVersion);
        this.sources = sources;
      }
      readNullBitmap(_type, nullCount, { offset } = this.nextBufferRange()) {
        return nullCount <= 0 ? new Uint8Array(0) : (0, bit_js_1.packBools)(this.sources[offset]);
      }
      readOffsets(_type, { offset } = this.nextBufferRange()) {
        return (0, buffer_js_1.toArrayBufferView)(Uint8Array, (0, buffer_js_1.toArrayBufferView)(_type.OffsetArrayType, this.sources[offset]));
      }
      readTypeIds(type, { offset } = this.nextBufferRange()) {
        return (0, buffer_js_1.toArrayBufferView)(Uint8Array, (0, buffer_js_1.toArrayBufferView)(type.ArrayType, this.sources[offset]));
      }
      readData(type, { offset } = this.nextBufferRange()) {
        const { sources } = this;
        if (type_js_1.DataType.isTimestamp(type)) {
          return (0, buffer_js_1.toArrayBufferView)(Uint8Array, int_js_1.Int64.convertArray(sources[offset]));
        } else if ((type_js_1.DataType.isInt(type) || type_js_1.DataType.isTime(type)) && type.bitWidth === 64 || type_js_1.DataType.isDuration(type)) {
          return (0, buffer_js_1.toArrayBufferView)(Uint8Array, int_js_1.Int64.convertArray(sources[offset]));
        } else if (type_js_1.DataType.isDate(type) && type.unit === enum_js_1.DateUnit.MILLISECOND) {
          return (0, buffer_js_1.toArrayBufferView)(Uint8Array, int_js_1.Int64.convertArray(sources[offset]));
        } else if (type_js_1.DataType.isDecimal(type)) {
          return (0, buffer_js_1.toArrayBufferView)(Uint8Array, int_js_1.Int128.convertArray(sources[offset]));
        } else if (type_js_1.DataType.isBinary(type) || type_js_1.DataType.isLargeBinary(type) || type_js_1.DataType.isFixedSizeBinary(type)) {
          return binaryDataFromJSON(sources[offset]);
        } else if (type_js_1.DataType.isBool(type)) {
          return (0, bit_js_1.packBools)(sources[offset]);
        } else if (type_js_1.DataType.isUtf8(type) || type_js_1.DataType.isLargeUtf8(type)) {
          return (0, utf8_js_1.encodeUtf8)(sources[offset].join(""));
        }
        return (0, buffer_js_1.toArrayBufferView)(Uint8Array, (0, buffer_js_1.toArrayBufferView)(type.ArrayType, sources[offset].map((x) => +x)));
      }
    };
    exports2.JSONVectorLoader = JSONVectorLoader;
    function binaryDataFromJSON(values) {
      const joined = values.join("");
      const data = new Uint8Array(joined.length / 2);
      for (let i = 0; i < joined.length; i += 2) {
        data[i >> 1] = Number.parseInt(joined.slice(i, i + 2), 16);
      }
      return data;
    }
  }
});

// node_modules/apache-arrow/builder/binary.js
var require_binary2 = __commonJS({
  "node_modules/apache-arrow/builder/binary.js"(exports2) {
    "use strict";
    Object.defineProperty(exports2, "__esModule", { value: true });
    exports2.BinaryBuilder = void 0;
    var buffer_js_1 = require_buffer3();
    var builder_js_1 = require_builder();
    var buffer_js_2 = require_buffer();
    var BinaryBuilder = class extends builder_js_1.VariableWidthBuilder {
      constructor(opts) {
        super(opts);
        this._values = new buffer_js_1.BufferBuilder(Uint8Array);
      }
      get byteLength() {
        let size = this._pendingLength + this.length * 4;
        this._offsets && (size += this._offsets.byteLength);
        this._values && (size += this._values.byteLength);
        this._nulls && (size += this._nulls.byteLength);
        return size;
      }
      setValue(index, value) {
        return super.setValue(index, (0, buffer_js_2.toUint8Array)(value));
      }
      _flushPending(pending, pendingLength) {
        const offsets = this._offsets;
        const data = this._values.reserve(pendingLength).buffer;
        let offset = 0;
        for (const [index, value] of pending) {
          if (value === void 0) {
            offsets.set(index, 0);
          } else {
            const length = value.length;
            data.set(value, offset);
            offsets.set(index, length);
            offset += length;
          }
        }
      }
    };
    exports2.BinaryBuilder = BinaryBuilder;
  }
});

// node_modules/apache-arrow/builder/largebinary.js
var require_largebinary = __commonJS({
  "node_modules/apache-arrow/builder/largebinary.js"(exports2) {
    "use strict";
    Object.defineProperty(exports2, "__esModule", { value: true });
    exports2.LargeBinaryBuilder = void 0;
    var buffer_js_1 = require_buffer();
    var buffer_js_2 = require_buffer3();
    var builder_js_1 = require_builder();
    var LargeBinaryBuilder = class extends builder_js_1.VariableWidthBuilder {
      constructor(opts) {
        super(opts);
        this._values = new buffer_js_2.BufferBuilder(Uint8Array);
      }
      get byteLength() {
        let size = this._pendingLength + this.length * 4;
        this._offsets && (size += this._offsets.byteLength);
        this._values && (size += this._values.byteLength);
        this._nulls && (size += this._nulls.byteLength);
        return size;
      }
      setValue(index, value) {
        return super.setValue(index, (0, buffer_js_1.toUint8Array)(value));
      }
      _flushPending(pending, pendingLength) {
        const offsets = this._offsets;
        const data = this._values.reserve(pendingLength).buffer;
        let offset = 0;
        for (const [index, value] of pending) {
          if (value === void 0) {
            offsets.set(index, BigInt(0));
          } else {
            const length = value.length;
            data.set(value, offset);
            offsets.set(index, BigInt(length));
            offset += length;
          }
        }
      }
    };
    exports2.LargeBinaryBuilder = LargeBinaryBuilder;
  }
});

// node_modules/apache-arrow/builder/bool.js
var require_bool2 = __commonJS({
  "node_modules/apache-arrow/builder/bool.js"(exports2) {
    "use strict";
    Object.defineProperty(exports2, "__esModule", { value: true });
    exports2.BoolBuilder = void 0;
    var buffer_js_1 = require_buffer3();
    var builder_js_1 = require_builder();
    var BoolBuilder = class extends builder_js_1.Builder {
      constructor(options) {
        super(options);
        this._values = new buffer_js_1.BitmapBufferBuilder();
      }
      setValue(index, value) {
        this._values.set(index, +value);
      }
    };
    exports2.BoolBuilder = BoolBuilder;
  }
});

// node_modules/apache-arrow/builder/date.js
var require_date2 = __commonJS({
  "node_modules/apache-arrow/builder/date.js"(exports2) {
    "use strict";
    Object.defineProperty(exports2, "__esModule", { value: true });
    exports2.DateMillisecondBuilder = exports2.DateDayBuilder = exports2.DateBuilder = void 0;
    var builder_js_1 = require_builder();
    var set_js_1 = require_set();
    var DateBuilder = class extends builder_js_1.FixedWidthBuilder {
    };
    exports2.DateBuilder = DateBuilder;
    DateBuilder.prototype._setValue = set_js_1.setDate;
    var DateDayBuilder = class extends DateBuilder {
    };
    exports2.DateDayBuilder = DateDayBuilder;
    DateDayBuilder.prototype._setValue = set_js_1.setDateDay;
    var DateMillisecondBuilder = class extends DateBuilder {
    };
    exports2.DateMillisecondBuilder = DateMillisecondBuilder;
    DateMillisecondBuilder.prototype._setValue = set_js_1.setDateMillisecond;
  }
});

// node_modules/apache-arrow/builder/decimal.js
var require_decimal2 = __commonJS({
  "node_modules/apache-arrow/builder/decimal.js"(exports2) {
    "use strict";
    Object.defineProperty(exports2, "__esModule", { value: true });
    exports2.DecimalBuilder = void 0;
    var builder_js_1 = require_builder();
    var set_js_1 = require_set();
    var DecimalBuilder = class extends builder_js_1.FixedWidthBuilder {
    };
    exports2.DecimalBuilder = DecimalBuilder;
    DecimalBuilder.prototype._setValue = set_js_1.setDecimal;
  }
});

// node_modules/apache-arrow/builder/dictionary.js
var require_dictionary = __commonJS({
  "node_modules/apache-arrow/builder/dictionary.js"(exports2) {
    "use strict";
    Object.defineProperty(exports2, "__esModule", { value: true });
    exports2.DictionaryBuilder = void 0;
    var type_js_1 = require_type2();
    var builder_js_1 = require_builder();
    var factories_js_1 = require_factories();
    var DictionaryBuilder = class extends builder_js_1.Builder {
      constructor({ "type": type, "nullValues": nulls, "dictionaryHashFunction": hashFn }) {
        super({ type: new type_js_1.Dictionary(type.dictionary, type.indices, type.id, type.isOrdered) });
        this._nulls = null;
        this._dictionaryOffset = 0;
        this._keysToIndices = /* @__PURE__ */ Object.create(null);
        this.indices = (0, factories_js_1.makeBuilder)({ "type": this.type.indices, "nullValues": nulls });
        this.dictionary = (0, factories_js_1.makeBuilder)({ "type": this.type.dictionary, "nullValues": null });
        if (typeof hashFn === "function") {
          this.valueToKey = hashFn;
        }
      }
      get values() {
        return this.indices.values;
      }
      get nullCount() {
        return this.indices.nullCount;
      }
      get nullBitmap() {
        return this.indices.nullBitmap;
      }
      get byteLength() {
        return this.indices.byteLength + this.dictionary.byteLength;
      }
      get reservedLength() {
        return this.indices.reservedLength + this.dictionary.reservedLength;
      }
      get reservedByteLength() {
        return this.indices.reservedByteLength + this.dictionary.reservedByteLength;
      }
      isValid(value) {
        return this.indices.isValid(value);
      }
      setValid(index, valid) {
        const indices = this.indices;
        valid = indices.setValid(index, valid);
        this.length = indices.length;
        return valid;
      }
      setValue(index, value) {
        const keysToIndices = this._keysToIndices;
        const key = this.valueToKey(value);
        let idx = keysToIndices[key];
        if (idx === void 0) {
          keysToIndices[key] = idx = this._dictionaryOffset + this.dictionary.append(value).length - 1;
        }
        return this.indices.setValue(index, idx);
      }
      flush() {
        const type = this.type;
        const prev = this._dictionary;
        const curr = this.dictionary.toVector();
        const data = this.indices.flush().clone(type);
        data.dictionary = prev ? prev.concat(curr) : curr;
        this.finished || (this._dictionaryOffset += curr.length);
        this._dictionary = data.dictionary;
        this.clear();
        return data;
      }
      finish() {
        this.indices.finish();
        this.dictionary.finish();
        this._dictionaryOffset = 0;
        this._keysToIndices = /* @__PURE__ */ Object.create(null);
        return super.finish();
      }
      clear() {
        this.indices.clear();
        this.dictionary.clear();
        return super.clear();
      }
      valueToKey(val) {
        return typeof val === "string" ? val : `${val}`;
      }
    };
    exports2.DictionaryBuilder = DictionaryBuilder;
  }
});

// node_modules/apache-arrow/builder/fixedsizebinary.js
var require_fixedsizebinary = __commonJS({
  "node_modules/apache-arrow/builder/fixedsizebinary.js"(exports2) {
    "use strict";
    Object.defineProperty(exports2, "__esModule", { value: true });
    exports2.FixedSizeBinaryBuilder = void 0;
    var builder_js_1 = require_builder();
    var set_js_1 = require_set();
    var FixedSizeBinaryBuilder = class extends builder_js_1.FixedWidthBuilder {
    };
    exports2.FixedSizeBinaryBuilder = FixedSizeBinaryBuilder;
    FixedSizeBinaryBuilder.prototype._setValue = set_js_1.setFixedSizeBinary;
  }
});

// node_modules/apache-arrow/builder/fixedsizelist.js
var require_fixedsizelist = __commonJS({
  "node_modules/apache-arrow/builder/fixedsizelist.js"(exports2) {
    "use strict";
    Object.defineProperty(exports2, "__esModule", { value: true });
    exports2.FixedSizeListBuilder = void 0;
    var schema_js_1 = require_schema2();
    var builder_js_1 = require_builder();
    var type_js_1 = require_type2();
    var FixedSizeListBuilder = class extends builder_js_1.Builder {
      setValue(index, value) {
        const [child] = this.children;
        const start = index * this.stride;
        for (let i = -1, n = value.length; ++i < n; ) {
          child.set(start + i, value[i]);
        }
      }
      addChild(child, name = "0") {
        if (this.numChildren > 0) {
          throw new Error("FixedSizeListBuilder can only have one child.");
        }
        const childIndex = this.children.push(child);
        this.type = new type_js_1.FixedSizeList(this.type.listSize, new schema_js_1.Field(name, child.type, true));
        return childIndex;
      }
    };
    exports2.FixedSizeListBuilder = FixedSizeListBuilder;
  }
});

// node_modules/apache-arrow/builder/float.js
var require_float = __commonJS({
  "node_modules/apache-arrow/builder/float.js"(exports2) {
    "use strict";
    Object.defineProperty(exports2, "__esModule", { value: true });
    exports2.Float64Builder = exports2.Float32Builder = exports2.Float16Builder = exports2.FloatBuilder = void 0;
    var math_js_1 = require_math();
    var builder_js_1 = require_builder();
    var FloatBuilder = class extends builder_js_1.FixedWidthBuilder {
      setValue(index, value) {
        this._values.set(index, value);
      }
    };
    exports2.FloatBuilder = FloatBuilder;
    var Float16Builder = class extends FloatBuilder {
      setValue(index, value) {
        super.setValue(index, (0, math_js_1.float64ToUint16)(value));
      }
    };
    exports2.Float16Builder = Float16Builder;
    var Float32Builder = class extends FloatBuilder {
    };
    exports2.Float32Builder = Float32Builder;
    var Float64Builder = class extends FloatBuilder {
    };
    exports2.Float64Builder = Float64Builder;
  }
});

// node_modules/apache-arrow/builder/interval.js
var require_interval2 = __commonJS({
  "node_modules/apache-arrow/builder/interval.js"(exports2) {
    "use strict";
    Object.defineProperty(exports2, "__esModule", { value: true });
    exports2.IntervalYearMonthBuilder = exports2.IntervalDayTimeBuilder = exports2.IntervalBuilder = void 0;
    var builder_js_1 = require_builder();
    var set_js_1 = require_set();
    var IntervalBuilder = class extends builder_js_1.FixedWidthBuilder {
    };
    exports2.IntervalBuilder = IntervalBuilder;
    IntervalBuilder.prototype._setValue = set_js_1.setIntervalValue;
    var IntervalDayTimeBuilder = class extends IntervalBuilder {
    };
    exports2.IntervalDayTimeBuilder = IntervalDayTimeBuilder;
    IntervalDayTimeBuilder.prototype._setValue = set_js_1.setIntervalDayTime;
    var IntervalYearMonthBuilder = class extends IntervalBuilder {
    };
    exports2.IntervalYearMonthBuilder = IntervalYearMonthBuilder;
    IntervalYearMonthBuilder.prototype._setValue = set_js_1.setIntervalYearMonth;
  }
});

// node_modules/apache-arrow/builder/duration.js
var require_duration2 = __commonJS({
  "node_modules/apache-arrow/builder/duration.js"(exports2) {
    "use strict";
    Object.defineProperty(exports2, "__esModule", { value: true });
    exports2.DurationNanosecondBuilder = exports2.DurationMicrosecondBuilder = exports2.DurationMillisecondBuilder = exports2.DurationSecondBuilder = exports2.DurationBuilder = void 0;
    var builder_js_1 = require_builder();
    var set_js_1 = require_set();
    var DurationBuilder = class extends builder_js_1.FixedWidthBuilder {
    };
    exports2.DurationBuilder = DurationBuilder;
    DurationBuilder.prototype._setValue = set_js_1.setDuration;
    var DurationSecondBuilder = class extends DurationBuilder {
    };
    exports2.DurationSecondBuilder = DurationSecondBuilder;
    DurationSecondBuilder.prototype._setValue = set_js_1.setDurationSecond;
    var DurationMillisecondBuilder = class extends DurationBuilder {
    };
    exports2.DurationMillisecondBuilder = DurationMillisecondBuilder;
    DurationMillisecondBuilder.prototype._setValue = set_js_1.setDurationMillisecond;
    var DurationMicrosecondBuilder = class extends DurationBuilder {
    };
    exports2.DurationMicrosecondBuilder = DurationMicrosecondBuilder;
    DurationMicrosecondBuilder.prototype._setValue = set_js_1.setDurationMicrosecond;
    var DurationNanosecondBuilder = class extends DurationBuilder {
    };
    exports2.DurationNanosecondBuilder = DurationNanosecondBuilder;
    DurationNanosecondBuilder.prototype._setValue = set_js_1.setDurationNanosecond;
  }
});

// node_modules/apache-arrow/builder/int.js
var require_int3 = __commonJS({
  "node_modules/apache-arrow/builder/int.js"(exports2) {
    "use strict";
    Object.defineProperty(exports2, "__esModule", { value: true });
    exports2.Uint64Builder = exports2.Uint32Builder = exports2.Uint16Builder = exports2.Uint8Builder = exports2.Int64Builder = exports2.Int32Builder = exports2.Int16Builder = exports2.Int8Builder = exports2.IntBuilder = void 0;
    var builder_js_1 = require_builder();
    var IntBuilder = class extends builder_js_1.FixedWidthBuilder {
      setValue(index, value) {
        this._values.set(index, value);
      }
    };
    exports2.IntBuilder = IntBuilder;
    var Int8Builder = class extends IntBuilder {
    };
    exports2.Int8Builder = Int8Builder;
    var Int16Builder = class extends IntBuilder {
    };
    exports2.Int16Builder = Int16Builder;
    var Int32Builder = class extends IntBuilder {
    };
    exports2.Int32Builder = Int32Builder;
    var Int64Builder = class extends IntBuilder {
    };
    exports2.Int64Builder = Int64Builder;
    var Uint8Builder = class extends IntBuilder {
    };
    exports2.Uint8Builder = Uint8Builder;
    var Uint16Builder = class extends IntBuilder {
    };
    exports2.Uint16Builder = Uint16Builder;
    var Uint32Builder = class extends IntBuilder {
    };
    exports2.Uint32Builder = Uint32Builder;
    var Uint64Builder = class extends IntBuilder {
    };
    exports2.Uint64Builder = Uint64Builder;
  }
});

// node_modules/apache-arrow/builder/list.js
var require_list2 = __commonJS({
  "node_modules/apache-arrow/builder/list.js"(exports2) {
    "use strict";
    Object.defineProperty(exports2, "__esModule", { value: true });
    exports2.ListBuilder = void 0;
    var schema_js_1 = require_schema2();
    var type_js_1 = require_type2();
    var buffer_js_1 = require_buffer3();
    var builder_js_1 = require_builder();
    var ListBuilder = class extends builder_js_1.VariableWidthBuilder {
      constructor(opts) {
        super(opts);
        this._offsets = new buffer_js_1.OffsetsBufferBuilder(opts.type);
      }
      addChild(child, name = "0") {
        if (this.numChildren > 0) {
          throw new Error("ListBuilder can only have one child.");
        }
        this.children[this.numChildren] = child;
        this.type = new type_js_1.List(new schema_js_1.Field(name, child.type, true));
        return this.numChildren - 1;
      }
      _flushPending(pending) {
        const offsets = this._offsets;
        const [child] = this.children;
        for (const [index, value] of pending) {
          if (typeof value === "undefined") {
            offsets.set(index, 0);
          } else {
            const v = value;
            const n = v.length;
            const start = offsets.set(index, n).buffer[index];
            for (let i = -1; ++i < n; ) {
              child.set(start + i, v[i]);
            }
          }
        }
      }
    };
    exports2.ListBuilder = ListBuilder;
  }
});

// node_modules/apache-arrow/builder/map.js
var require_map3 = __commonJS({
  "node_modules/apache-arrow/builder/map.js"(exports2) {
    "use strict";
    Object.defineProperty(exports2, "__esModule", { value: true });
    exports2.MapBuilder = void 0;
    var schema_js_1 = require_schema2();
    var type_js_1 = require_type2();
    var builder_js_1 = require_builder();
    var MapBuilder = class extends builder_js_1.VariableWidthBuilder {
      set(index, value) {
        return super.set(index, value);
      }
      setValue(index, value) {
        const row = value instanceof Map ? value : new Map(Object.entries(value));
        const pending = this._pending || (this._pending = /* @__PURE__ */ new Map());
        const current = pending.get(index);
        current && (this._pendingLength -= current.size);
        this._pendingLength += row.size;
        pending.set(index, row);
      }
      addChild(child, name = `${this.numChildren}`) {
        if (this.numChildren > 0) {
          throw new Error("ListBuilder can only have one child.");
        }
        this.children[this.numChildren] = child;
        this.type = new type_js_1.Map_(new schema_js_1.Field(name, child.type, true), this.type.keysSorted);
        return this.numChildren - 1;
      }
      _flushPending(pending) {
        const offsets = this._offsets;
        const [child] = this.children;
        for (const [index, value] of pending) {
          if (value === void 0) {
            offsets.set(index, 0);
          } else {
            let { [index]: idx, [index + 1]: end } = offsets.set(index, value.size).buffer;
            for (const val of value.entries()) {
              child.set(idx, val);
              if (++idx >= end)
                break;
            }
          }
        }
      }
    };
    exports2.MapBuilder = MapBuilder;
  }
});

// node_modules/apache-arrow/builder/null.js
var require_null2 = __commonJS({
  "node_modules/apache-arrow/builder/null.js"(exports2) {
    "use strict";
    Object.defineProperty(exports2, "__esModule", { value: true });
    exports2.NullBuilder = void 0;
    var builder_js_1 = require_builder();
    var NullBuilder = class extends builder_js_1.Builder {
      // @ts-ignore
      setValue(index, value) {
      }
      setValid(index, valid) {
        this.length = Math.max(index + 1, this.length);
        return valid;
      }
    };
    exports2.NullBuilder = NullBuilder;
  }
});

// node_modules/apache-arrow/builder/struct.js
var require_struct3 = __commonJS({
  "node_modules/apache-arrow/builder/struct.js"(exports2) {
    "use strict";
    Object.defineProperty(exports2, "__esModule", { value: true });
    exports2.StructBuilder = void 0;
    var schema_js_1 = require_schema2();
    var builder_js_1 = require_builder();
    var type_js_1 = require_type2();
    var StructBuilder = class extends builder_js_1.Builder {
      setValue(index, value) {
        const { children, type } = this;
        switch (Array.isArray(value) || value.constructor) {
          case true:
            return type.children.forEach((_, i) => children[i].set(index, value[i]));
          case Map:
            return type.children.forEach((f, i) => children[i].set(index, value.get(f.name)));
          default:
            return type.children.forEach((f, i) => children[i].set(index, value[f.name]));
        }
      }
      /** @inheritdoc */
      setValid(index, valid) {
        if (!super.setValid(index, valid)) {
          this.children.forEach((child) => child.setValid(index, valid));
        }
        return valid;
      }
      addChild(child, name = `${this.numChildren}`) {
        const childIndex = this.children.push(child);
        this.type = new type_js_1.Struct([...this.type.children, new schema_js_1.Field(name, child.type, true)]);
        return childIndex;
      }
    };
    exports2.StructBuilder = StructBuilder;
  }
});

// node_modules/apache-arrow/builder/timestamp.js
var require_timestamp2 = __commonJS({
  "node_modules/apache-arrow/builder/timestamp.js"(exports2) {
    "use strict";
    Object.defineProperty(exports2, "__esModule", { value: true });
    exports2.TimestampNanosecondBuilder = exports2.TimestampMicrosecondBuilder = exports2.TimestampMillisecondBuilder = exports2.TimestampSecondBuilder = exports2.TimestampBuilder = void 0;
    var builder_js_1 = require_builder();
    var set_js_1 = require_set();
    var TimestampBuilder = class extends builder_js_1.FixedWidthBuilder {
    };
    exports2.TimestampBuilder = TimestampBuilder;
    TimestampBuilder.prototype._setValue = set_js_1.setTimestamp;
    var TimestampSecondBuilder = class extends TimestampBuilder {
    };
    exports2.TimestampSecondBuilder = TimestampSecondBuilder;
    TimestampSecondBuilder.prototype._setValue = set_js_1.setTimestampSecond;
    var TimestampMillisecondBuilder = class extends TimestampBuilder {
    };
    exports2.TimestampMillisecondBuilder = TimestampMillisecondBuilder;
    TimestampMillisecondBuilder.prototype._setValue = set_js_1.setTimestampMillisecond;
    var TimestampMicrosecondBuilder = class extends TimestampBuilder {
    };
    exports2.TimestampMicrosecondBuilder = TimestampMicrosecondBuilder;
    TimestampMicrosecondBuilder.prototype._setValue = set_js_1.setTimestampMicrosecond;
    var TimestampNanosecondBuilder = class extends TimestampBuilder {
    };
    exports2.TimestampNanosecondBuilder = TimestampNanosecondBuilder;
    TimestampNanosecondBuilder.prototype._setValue = set_js_1.setTimestampNanosecond;
  }
});

// node_modules/apache-arrow/builder/time.js
var require_time2 = __commonJS({
  "node_modules/apache-arrow/builder/time.js"(exports2) {
    "use strict";
    Object.defineProperty(exports2, "__esModule", { value: true });
    exports2.TimeNanosecondBuilder = exports2.TimeMicrosecondBuilder = exports2.TimeMillisecondBuilder = exports2.TimeSecondBuilder = exports2.TimeBuilder = void 0;
    var builder_js_1 = require_builder();
    var set_js_1 = require_set();
    var TimeBuilder = class extends builder_js_1.FixedWidthBuilder {
    };
    exports2.TimeBuilder = TimeBuilder;
    TimeBuilder.prototype._setValue = set_js_1.setTime;
    var TimeSecondBuilder = class extends TimeBuilder {
    };
    exports2.TimeSecondBuilder = TimeSecondBuilder;
    TimeSecondBuilder.prototype._setValue = set_js_1.setTimeSecond;
    var TimeMillisecondBuilder = class extends TimeBuilder {
    };
    exports2.TimeMillisecondBuilder = TimeMillisecondBuilder;
    TimeMillisecondBuilder.prototype._setValue = set_js_1.setTimeMillisecond;
    var TimeMicrosecondBuilder = class extends TimeBuilder {
    };
    exports2.TimeMicrosecondBuilder = TimeMicrosecondBuilder;
    TimeMicrosecondBuilder.prototype._setValue = set_js_1.setTimeMicrosecond;
    var TimeNanosecondBuilder = class extends TimeBuilder {
    };
    exports2.TimeNanosecondBuilder = TimeNanosecondBuilder;
    TimeNanosecondBuilder.prototype._setValue = set_js_1.setTimeNanosecond;
  }
});

// node_modules/apache-arrow/builder/union.js
var require_union2 = __commonJS({
  "node_modules/apache-arrow/builder/union.js"(exports2) {
    "use strict";
    Object.defineProperty(exports2, "__esModule", { value: true });
    exports2.DenseUnionBuilder = exports2.SparseUnionBuilder = exports2.UnionBuilder = void 0;
    var schema_js_1 = require_schema2();
    var buffer_js_1 = require_buffer3();
    var builder_js_1 = require_builder();
    var type_js_1 = require_type2();
    var UnionBuilder = class extends builder_js_1.Builder {
      constructor(options) {
        super(options);
        this._typeIds = new buffer_js_1.DataBufferBuilder(Int8Array, 0, 1);
        if (typeof options["valueToChildTypeId"] === "function") {
          this._valueToChildTypeId = options["valueToChildTypeId"];
        }
      }
      get typeIdToChildIndex() {
        return this.type.typeIdToChildIndex;
      }
      append(value, childTypeId) {
        return this.set(this.length, value, childTypeId);
      }
      set(index, value, childTypeId) {
        if (childTypeId === void 0) {
          childTypeId = this._valueToChildTypeId(this, value, index);
        }
        this.setValue(index, value, childTypeId);
        return this;
      }
      setValue(index, value, childTypeId) {
        this._typeIds.set(index, childTypeId);
        const childIndex = this.type.typeIdToChildIndex[childTypeId];
        const child = this.children[childIndex];
        child === null || child === void 0 ? void 0 : child.set(index, value);
      }
      addChild(child, name = `${this.children.length}`) {
        const childTypeId = this.children.push(child);
        const { type: { children, mode, typeIds } } = this;
        const fields = [...children, new schema_js_1.Field(name, child.type)];
        this.type = new type_js_1.Union(mode, [...typeIds, childTypeId], fields);
        return childTypeId;
      }
      /** @ignore */
      // @ts-ignore
      _valueToChildTypeId(builder, value, offset) {
        throw new Error(`Cannot map UnionBuilder value to child typeId. Pass the \`childTypeId\` as the second argument to unionBuilder.append(), or supply a \`valueToChildTypeId\` function as part of the UnionBuilder constructor options.`);
      }
    };
    exports2.UnionBuilder = UnionBuilder;
    var SparseUnionBuilder = class extends UnionBuilder {
    };
    exports2.SparseUnionBuilder = SparseUnionBuilder;
    var DenseUnionBuilder = class extends UnionBuilder {
      constructor(options) {
        super(options);
        this._offsets = new buffer_js_1.DataBufferBuilder(Int32Array);
      }
      /** @ignore */
      setValue(index, value, childTypeId) {
        const id = this._typeIds.set(index, childTypeId).buffer[index];
        const child = this.getChildAt(this.type.typeIdToChildIndex[id]);
        const denseIndex = this._offsets.set(index, child.length).buffer[index];
        child === null || child === void 0 ? void 0 : child.set(denseIndex, value);
      }
    };
    exports2.DenseUnionBuilder = DenseUnionBuilder;
  }
});

// node_modules/apache-arrow/builder/utf8.js
var require_utf83 = __commonJS({
  "node_modules/apache-arrow/builder/utf8.js"(exports2) {
    "use strict";
    Object.defineProperty(exports2, "__esModule", { value: true });
    exports2.Utf8Builder = void 0;
    var utf8_js_1 = require_utf8();
    var binary_js_1 = require_binary2();
    var buffer_js_1 = require_buffer3();
    var builder_js_1 = require_builder();
    var Utf8Builder = class extends builder_js_1.VariableWidthBuilder {
      constructor(opts) {
        super(opts);
        this._values = new buffer_js_1.BufferBuilder(Uint8Array);
      }
      get byteLength() {
        let size = this._pendingLength + this.length * 4;
        this._offsets && (size += this._offsets.byteLength);
        this._values && (size += this._values.byteLength);
        this._nulls && (size += this._nulls.byteLength);
        return size;
      }
      setValue(index, value) {
        return super.setValue(index, (0, utf8_js_1.encodeUtf8)(value));
      }
      // @ts-ignore
      _flushPending(pending, pendingLength) {
      }
    };
    exports2.Utf8Builder = Utf8Builder;
    Utf8Builder.prototype._flushPending = binary_js_1.BinaryBuilder.prototype._flushPending;
  }
});

// node_modules/apache-arrow/builder/largeutf8.js
var require_largeutf8 = __commonJS({
  "node_modules/apache-arrow/builder/largeutf8.js"(exports2) {
    "use strict";
    Object.defineProperty(exports2, "__esModule", { value: true });
    exports2.LargeUtf8Builder = void 0;
    var utf8_js_1 = require_utf8();
    var buffer_js_1 = require_buffer3();
    var builder_js_1 = require_builder();
    var largebinary_js_1 = require_largebinary();
    var LargeUtf8Builder = class extends builder_js_1.VariableWidthBuilder {
      constructor(opts) {
        super(opts);
        this._values = new buffer_js_1.BufferBuilder(Uint8Array);
      }
      get byteLength() {
        let size = this._pendingLength + this.length * 4;
        this._offsets && (size += this._offsets.byteLength);
        this._values && (size += this._values.byteLength);
        this._nulls && (size += this._nulls.byteLength);
        return size;
      }
      setValue(index, value) {
        return super.setValue(index, (0, utf8_js_1.encodeUtf8)(value));
      }
      // @ts-ignore
      _flushPending(pending, pendingLength) {
      }
    };
    exports2.LargeUtf8Builder = LargeUtf8Builder;
    LargeUtf8Builder.prototype._flushPending = largebinary_js_1.LargeBinaryBuilder.prototype._flushPending;
  }
});

// node_modules/apache-arrow/visitor/builderctor.js
var require_builderctor = __commonJS({
  "node_modules/apache-arrow/visitor/builderctor.js"(exports2) {
    "use strict";
    Object.defineProperty(exports2, "__esModule", { value: true });
    exports2.instance = exports2.GetBuilderCtor = void 0;
    var visitor_js_1 = require_visitor();
    var binary_js_1 = require_binary2();
    var largebinary_js_1 = require_largebinary();
    var bool_js_1 = require_bool2();
    var date_js_1 = require_date2();
    var decimal_js_1 = require_decimal2();
    var dictionary_js_1 = require_dictionary();
    var fixedsizebinary_js_1 = require_fixedsizebinary();
    var fixedsizelist_js_1 = require_fixedsizelist();
    var float_js_1 = require_float();
    var interval_js_1 = require_interval2();
    var duration_js_1 = require_duration2();
    var int_js_1 = require_int3();
    var list_js_1 = require_list2();
    var map_js_1 = require_map3();
    var null_js_1 = require_null2();
    var struct_js_1 = require_struct3();
    var timestamp_js_1 = require_timestamp2();
    var time_js_1 = require_time2();
    var union_js_1 = require_union2();
    var utf8_js_1 = require_utf83();
    var largeutf8_js_1 = require_largeutf8();
    var GetBuilderCtor = class extends visitor_js_1.Visitor {
      visitNull() {
        return null_js_1.NullBuilder;
      }
      visitBool() {
        return bool_js_1.BoolBuilder;
      }
      visitInt() {
        return int_js_1.IntBuilder;
      }
      visitInt8() {
        return int_js_1.Int8Builder;
      }
      visitInt16() {
        return int_js_1.Int16Builder;
      }
      visitInt32() {
        return int_js_1.Int32Builder;
      }
      visitInt64() {
        return int_js_1.Int64Builder;
      }
      visitUint8() {
        return int_js_1.Uint8Builder;
      }
      visitUint16() {
        return int_js_1.Uint16Builder;
      }
      visitUint32() {
        return int_js_1.Uint32Builder;
      }
      visitUint64() {
        return int_js_1.Uint64Builder;
      }
      visitFloat() {
        return float_js_1.FloatBuilder;
      }
      visitFloat16() {
        return float_js_1.Float16Builder;
      }
      visitFloat32() {
        return float_js_1.Float32Builder;
      }
      visitFloat64() {
        return float_js_1.Float64Builder;
      }
      visitUtf8() {
        return utf8_js_1.Utf8Builder;
      }
      visitLargeUtf8() {
        return largeutf8_js_1.LargeUtf8Builder;
      }
      visitBinary() {
        return binary_js_1.BinaryBuilder;
      }
      visitLargeBinary() {
        return largebinary_js_1.LargeBinaryBuilder;
      }
      visitFixedSizeBinary() {
        return fixedsizebinary_js_1.FixedSizeBinaryBuilder;
      }
      visitDate() {
        return date_js_1.DateBuilder;
      }
      visitDateDay() {
        return date_js_1.DateDayBuilder;
      }
      visitDateMillisecond() {
        return date_js_1.DateMillisecondBuilder;
      }
      visitTimestamp() {
        return timestamp_js_1.TimestampBuilder;
      }
      visitTimestampSecond() {
        return timestamp_js_1.TimestampSecondBuilder;
      }
      visitTimestampMillisecond() {
        return timestamp_js_1.TimestampMillisecondBuilder;
      }
      visitTimestampMicrosecond() {
        return timestamp_js_1.TimestampMicrosecondBuilder;
      }
      visitTimestampNanosecond() {
        return timestamp_js_1.TimestampNanosecondBuilder;
      }
      visitTime() {
        return time_js_1.TimeBuilder;
      }
      visitTimeSecond() {
        return time_js_1.TimeSecondBuilder;
      }
      visitTimeMillisecond() {
        return time_js_1.TimeMillisecondBuilder;
      }
      visitTimeMicrosecond() {
        return time_js_1.TimeMicrosecondBuilder;
      }
      visitTimeNanosecond() {
        return time_js_1.TimeNanosecondBuilder;
      }
      visitDecimal() {
        return decimal_js_1.DecimalBuilder;
      }
      visitList() {
        return list_js_1.ListBuilder;
      }
      visitStruct() {
        return struct_js_1.StructBuilder;
      }
      visitUnion() {
        return union_js_1.UnionBuilder;
      }
      visitDenseUnion() {
        return union_js_1.DenseUnionBuilder;
      }
      visitSparseUnion() {
        return union_js_1.SparseUnionBuilder;
      }
      visitDictionary() {
        return dictionary_js_1.DictionaryBuilder;
      }
      visitInterval() {
        return interval_js_1.IntervalBuilder;
      }
      visitIntervalDayTime() {
        return interval_js_1.IntervalDayTimeBuilder;
      }
      visitIntervalYearMonth() {
        return interval_js_1.IntervalYearMonthBuilder;
      }
      visitDuration() {
        return duration_js_1.DurationBuilder;
      }
      visitDurationSecond() {
        return duration_js_1.DurationSecondBuilder;
      }
      visitDurationMillisecond() {
        return duration_js_1.DurationMillisecondBuilder;
      }
      visitDurationMicrosecond() {
        return duration_js_1.DurationMicrosecondBuilder;
      }
      visitDurationNanosecond() {
        return duration_js_1.DurationNanosecondBuilder;
      }
      visitFixedSizeList() {
        return fixedsizelist_js_1.FixedSizeListBuilder;
      }
      visitMap() {
        return map_js_1.MapBuilder;
      }
    };
    exports2.GetBuilderCtor = GetBuilderCtor;
    exports2.instance = new GetBuilderCtor();
  }
});

// node_modules/apache-arrow/visitor/typecomparator.js
var require_typecomparator = __commonJS({
  "node_modules/apache-arrow/visitor/typecomparator.js"(exports2) {
    "use strict";
    Object.defineProperty(exports2, "__esModule", { value: true });
    exports2.compareTypes = exports2.compareFields = exports2.compareSchemas = exports2.instance = exports2.TypeComparator = void 0;
    var visitor_js_1 = require_visitor();
    var TypeComparator = class extends visitor_js_1.Visitor {
      compareSchemas(schema, other) {
        return schema === other || other instanceof schema.constructor && this.compareManyFields(schema.fields, other.fields);
      }
      compareManyFields(fields, others) {
        return fields === others || Array.isArray(fields) && Array.isArray(others) && fields.length === others.length && fields.every((f, i) => this.compareFields(f, others[i]));
      }
      compareFields(field, other) {
        return field === other || other instanceof field.constructor && field.name === other.name && field.nullable === other.nullable && this.visit(field.type, other.type);
      }
    };
    exports2.TypeComparator = TypeComparator;
    function compareConstructor(type, other) {
      return other instanceof type.constructor;
    }
    function compareAny(type, other) {
      return type === other || compareConstructor(type, other);
    }
    function compareInt(type, other) {
      return type === other || compareConstructor(type, other) && type.bitWidth === other.bitWidth && type.isSigned === other.isSigned;
    }
    function compareFloat(type, other) {
      return type === other || compareConstructor(type, other) && type.precision === other.precision;
    }
    function compareFixedSizeBinary(type, other) {
      return type === other || compareConstructor(type, other) && type.byteWidth === other.byteWidth;
    }
    function compareDate(type, other) {
      return type === other || compareConstructor(type, other) && type.unit === other.unit;
    }
    function compareTimestamp(type, other) {
      return type === other || compareConstructor(type, other) && type.unit === other.unit && type.timezone === other.timezone;
    }
    function compareTime(type, other) {
      return type === other || compareConstructor(type, other) && type.unit === other.unit && type.bitWidth === other.bitWidth;
    }
    function compareList(type, other) {
      return type === other || compareConstructor(type, other) && type.children.length === other.children.length && exports2.instance.compareManyFields(type.children, other.children);
    }
    function compareStruct(type, other) {
      return type === other || compareConstructor(type, other) && type.children.length === other.children.length && exports2.instance.compareManyFields(type.children, other.children);
    }
    function compareUnion(type, other) {
      return type === other || compareConstructor(type, other) && type.mode === other.mode && type.typeIds.every((x, i) => x === other.typeIds[i]) && exports2.instance.compareManyFields(type.children, other.children);
    }
    function compareDictionary(type, other) {
      return type === other || compareConstructor(type, other) && type.id === other.id && type.isOrdered === other.isOrdered && exports2.instance.visit(type.indices, other.indices) && exports2.instance.visit(type.dictionary, other.dictionary);
    }
    function compareInterval(type, other) {
      return type === other || compareConstructor(type, other) && type.unit === other.unit;
    }
    function compareDuration(type, other) {
      return type === other || compareConstructor(type, other) && type.unit === other.unit;
    }
    function compareFixedSizeList(type, other) {
      return type === other || compareConstructor(type, other) && type.listSize === other.listSize && type.children.length === other.children.length && exports2.instance.compareManyFields(type.children, other.children);
    }
    function compareMap(type, other) {
      return type === other || compareConstructor(type, other) && type.keysSorted === other.keysSorted && type.children.length === other.children.length && exports2.instance.compareManyFields(type.children, other.children);
    }
    TypeComparator.prototype.visitNull = compareAny;
    TypeComparator.prototype.visitBool = compareAny;
    TypeComparator.prototype.visitInt = compareInt;
    TypeComparator.prototype.visitInt8 = compareInt;
    TypeComparator.prototype.visitInt16 = compareInt;
    TypeComparator.prototype.visitInt32 = compareInt;
    TypeComparator.prototype.visitInt64 = compareInt;
    TypeComparator.prototype.visitUint8 = compareInt;
    TypeComparator.prototype.visitUint16 = compareInt;
    TypeComparator.prototype.visitUint32 = compareInt;
    TypeComparator.prototype.visitUint64 = compareInt;
    TypeComparator.prototype.visitFloat = compareFloat;
    TypeComparator.prototype.visitFloat16 = compareFloat;
    TypeComparator.prototype.visitFloat32 = compareFloat;
    TypeComparator.prototype.visitFloat64 = compareFloat;
    TypeComparator.prototype.visitUtf8 = compareAny;
    TypeComparator.prototype.visitLargeUtf8 = compareAny;
    TypeComparator.prototype.visitBinary = compareAny;
    TypeComparator.prototype.visitLargeBinary = compareAny;
    TypeComparator.prototype.visitFixedSizeBinary = compareFixedSizeBinary;
    TypeComparator.prototype.visitDate = compareDate;
    TypeComparator.prototype.visitDateDay = compareDate;
    TypeComparator.prototype.visitDateMillisecond = compareDate;
    TypeComparator.prototype.visitTimestamp = compareTimestamp;
    TypeComparator.prototype.visitTimestampSecond = compareTimestamp;
    TypeComparator.prototype.visitTimestampMillisecond = compareTimestamp;
    TypeComparator.prototype.visitTimestampMicrosecond = compareTimestamp;
    TypeComparator.prototype.visitTimestampNanosecond = compareTimestamp;
    TypeComparator.prototype.visitTime = compareTime;
    TypeComparator.prototype.visitTimeSecond = compareTime;
    TypeComparator.prototype.visitTimeMillisecond = compareTime;
    TypeComparator.prototype.visitTimeMicrosecond = compareTime;
    TypeComparator.prototype.visitTimeNanosecond = compareTime;
    TypeComparator.prototype.visitDecimal = compareAny;
    TypeComparator.prototype.visitList = compareList;
    TypeComparator.prototype.visitStruct = compareStruct;
    TypeComparator.prototype.visitUnion = compareUnion;
    TypeComparator.prototype.visitDenseUnion = compareUnion;
    TypeComparator.prototype.visitSparseUnion = compareUnion;
    TypeComparator.prototype.visitDictionary = compareDictionary;
    TypeComparator.prototype.visitInterval = compareInterval;
    TypeComparator.prototype.visitIntervalDayTime = compareInterval;
    TypeComparator.prototype.visitIntervalYearMonth = compareInterval;
    TypeComparator.prototype.visitDuration = compareDuration;
    TypeComparator.prototype.visitDurationSecond = compareDuration;
    TypeComparator.prototype.visitDurationMillisecond = compareDuration;
    TypeComparator.prototype.visitDurationMicrosecond = compareDuration;
    TypeComparator.prototype.visitDurationNanosecond = compareDuration;
    TypeComparator.prototype.visitFixedSizeList = compareFixedSizeList;
    TypeComparator.prototype.visitMap = compareMap;
    exports2.instance = new TypeComparator();
    function compareSchemas(schema, other) {
      return exports2.instance.compareSchemas(schema, other);
    }
    exports2.compareSchemas = compareSchemas;
    function compareFields(field, other) {
      return exports2.instance.compareFields(field, other);
    }
    exports2.compareFields = compareFields;
    function compareTypes(type, other) {
      return exports2.instance.visit(type, other);
    }
    exports2.compareTypes = compareTypes;
  }
});

// node_modules/apache-arrow/factories.js
var require_factories = __commonJS({
  "node_modules/apache-arrow/factories.js"(exports2) {
    "use strict";
    Object.defineProperty(exports2, "__esModule", { value: true });
    exports2.builderThroughAsyncIterable = exports2.builderThroughIterable = exports2.tableFromJSON = exports2.vectorFromArray = exports2.makeBuilder = void 0;
    var tslib_1 = (init_tslib_es6(), __toCommonJS(tslib_es6_exports));
    var schema_js_1 = require_schema2();
    var dtypes = require_type2();
    var data_js_1 = require_data();
    var vector_js_1 = require_vector2();
    var builderctor_js_1 = require_builderctor();
    var table_js_1 = require_table();
    var recordbatch_js_1 = require_recordbatch2();
    var typecomparator_js_1 = require_typecomparator();
    function makeBuilder(options) {
      const type = options.type;
      const builder = new (builderctor_js_1.instance.getVisitFn(type)())(options);
      if (type.children && type.children.length > 0) {
        const children = options["children"] || [];
        const defaultOptions = { "nullValues": options["nullValues"] };
        const getChildOptions = Array.isArray(children) ? ((_, i) => children[i] || defaultOptions) : (({ name }) => children[name] || defaultOptions);
        for (const [index, field] of type.children.entries()) {
          const { type: type2 } = field;
          const opts = getChildOptions(field, index);
          builder.children.push(makeBuilder(Object.assign(Object.assign({}, opts), { type: type2 })));
        }
      }
      return builder;
    }
    exports2.makeBuilder = makeBuilder;
    function vectorFromArray(init, type) {
      if (init instanceof data_js_1.Data || init instanceof vector_js_1.Vector || init.type instanceof dtypes.DataType || ArrayBuffer.isView(init)) {
        return (0, vector_js_1.makeVector)(init);
      }
      const options = { type: type !== null && type !== void 0 ? type : inferType(init), nullValues: [null] };
      const chunks = [...builderThroughIterable(options)(init)];
      const vector = chunks.length === 1 ? chunks[0] : chunks.reduce((a, b) => a.concat(b));
      if (dtypes.DataType.isDictionary(vector.type)) {
        return vector.memoize();
      }
      return vector;
    }
    exports2.vectorFromArray = vectorFromArray;
    function tableFromJSON(array) {
      const vector = vectorFromArray(array);
      const batch = new recordbatch_js_1.RecordBatch(new schema_js_1.Schema(vector.type.children), vector.data[0]);
      return new table_js_1.Table(batch);
    }
    exports2.tableFromJSON = tableFromJSON;
    function inferType(value) {
      if (value.length === 0) {
        return new dtypes.Null();
      }
      let nullsCount = 0;
      let arraysCount = 0;
      let objectsCount = 0;
      let numbersCount = 0;
      let stringsCount = 0;
      let bigintsCount = 0;
      let booleansCount = 0;
      let datesCount = 0;
      for (const val of value) {
        if (val == null) {
          ++nullsCount;
          continue;
        }
        switch (typeof val) {
          case "bigint":
            ++bigintsCount;
            continue;
          case "boolean":
            ++booleansCount;
            continue;
          case "number":
            ++numbersCount;
            continue;
          case "string":
            ++stringsCount;
            continue;
          case "object":
            if (Array.isArray(val)) {
              ++arraysCount;
            } else if (Object.prototype.toString.call(val) === "[object Date]") {
              ++datesCount;
            } else {
              ++objectsCount;
            }
            continue;
        }
        throw new TypeError("Unable to infer Vector type from input values, explicit type declaration expected.");
      }
      if (numbersCount + nullsCount === value.length) {
        return new dtypes.Float64();
      } else if (stringsCount + nullsCount === value.length) {
        return new dtypes.Dictionary(new dtypes.Utf8(), new dtypes.Int32());
      } else if (bigintsCount + nullsCount === value.length) {
        return new dtypes.Int64();
      } else if (booleansCount + nullsCount === value.length) {
        return new dtypes.Bool();
      } else if (datesCount + nullsCount === value.length) {
        return new dtypes.TimestampMillisecond();
      } else if (arraysCount + nullsCount === value.length) {
        const array = value;
        const childType = inferType(array[array.findIndex((ary) => ary != null)]);
        if (array.every((ary) => ary == null || (0, typecomparator_js_1.compareTypes)(childType, inferType(ary)))) {
          return new dtypes.List(new schema_js_1.Field("", childType, true));
        }
      } else if (objectsCount + nullsCount === value.length) {
        const fields = /* @__PURE__ */ new Map();
        for (const row of value) {
          for (const key of Object.keys(row)) {
            if (!fields.has(key) && row[key] != null) {
              fields.set(key, new schema_js_1.Field(key, inferType([row[key]]), true));
            }
          }
        }
        return new dtypes.Struct([...fields.values()]);
      }
      throw new TypeError("Unable to infer Vector type from input values, explicit type declaration expected.");
    }
    function builderThroughIterable(options) {
      const { ["queueingStrategy"]: queueingStrategy = "count" } = options;
      const { ["highWaterMark"]: highWaterMark = queueingStrategy !== "bytes" ? Number.POSITIVE_INFINITY : Math.pow(2, 14) } = options;
      const sizeProperty = queueingStrategy !== "bytes" ? "length" : "byteLength";
      return function* (source) {
        let numChunks = 0;
        const builder = makeBuilder(options);
        for (const value of source) {
          if (builder.append(value)[sizeProperty] >= highWaterMark) {
            ++numChunks && (yield builder.toVector());
          }
        }
        if (builder.finish().length > 0 || numChunks === 0) {
          yield builder.toVector();
        }
      };
    }
    exports2.builderThroughIterable = builderThroughIterable;
    function builderThroughAsyncIterable(options) {
      const { ["queueingStrategy"]: queueingStrategy = "count" } = options;
      const { ["highWaterMark"]: highWaterMark = queueingStrategy !== "bytes" ? Number.POSITIVE_INFINITY : Math.pow(2, 14) } = options;
      const sizeProperty = queueingStrategy !== "bytes" ? "length" : "byteLength";
      return function(source) {
        return tslib_1.__asyncGenerator(this, arguments, function* () {
          var _a, e_1, _b, _c;
          let numChunks = 0;
          const builder = makeBuilder(options);
          try {
            for (var _d = true, source_1 = tslib_1.__asyncValues(source), source_1_1; source_1_1 = yield tslib_1.__await(source_1.next()), _a = source_1_1.done, !_a; _d = true) {
              _c = source_1_1.value;
              _d = false;
              const value = _c;
              if (builder.append(value)[sizeProperty] >= highWaterMark) {
                ++numChunks && (yield yield tslib_1.__await(builder.toVector()));
              }
            }
          } catch (e_1_1) {
            e_1 = { error: e_1_1 };
          } finally {
            try {
              if (!_d && !_a && (_b = source_1.return)) yield tslib_1.__await(_b.call(source_1));
            } finally {
              if (e_1) throw e_1.error;
            }
          }
          if (builder.finish().length > 0 || numChunks === 0) {
            yield yield tslib_1.__await(builder.toVector());
          }
        });
      };
    }
    exports2.builderThroughAsyncIterable = builderThroughAsyncIterable;
  }
});

// node_modules/apache-arrow/util/recordbatch.js
var require_recordbatch = __commonJS({
  "node_modules/apache-arrow/util/recordbatch.js"(exports2) {
    "use strict";
    Object.defineProperty(exports2, "__esModule", { value: true });
    exports2.distributeVectorsIntoRecordBatches = void 0;
    var data_js_1 = require_data();
    var type_js_1 = require_type2();
    var recordbatch_js_1 = require_recordbatch2();
    function distributeVectorsIntoRecordBatches(schema, vecs) {
      return uniformlyDistributeChunksAcrossRecordBatches(schema, vecs.map((v) => v.data.concat()));
    }
    exports2.distributeVectorsIntoRecordBatches = distributeVectorsIntoRecordBatches;
    function uniformlyDistributeChunksAcrossRecordBatches(schema, cols) {
      const fields = [...schema.fields];
      const batches = [];
      const memo = { numBatches: cols.reduce((n, c) => Math.max(n, c.length), 0) };
      let numBatches = 0, batchLength = 0;
      let i = -1;
      const numColumns = cols.length;
      let child, children = [];
      while (memo.numBatches-- > 0) {
        for (batchLength = Number.POSITIVE_INFINITY, i = -1; ++i < numColumns; ) {
          children[i] = child = cols[i].shift();
          batchLength = Math.min(batchLength, child ? child.length : batchLength);
        }
        if (Number.isFinite(batchLength)) {
          children = distributeChildren(fields, batchLength, children, cols, memo);
          if (batchLength > 0) {
            batches[numBatches++] = (0, data_js_1.makeData)({
              type: new type_js_1.Struct(fields),
              length: batchLength,
              nullCount: 0,
              children: children.slice()
            });
          }
        }
      }
      return [
        schema = schema.assign(fields),
        batches.map((data) => new recordbatch_js_1.RecordBatch(schema, data))
      ];
    }
    function distributeChildren(fields, batchLength, children, columns, memo) {
      var _a;
      const nullBitmapSize = (batchLength + 63 & ~63) >> 3;
      for (let i = -1, n = columns.length; ++i < n; ) {
        const child = children[i];
        const length = child === null || child === void 0 ? void 0 : child.length;
        if (length >= batchLength) {
          if (length === batchLength) {
            children[i] = child;
          } else {
            children[i] = child.slice(0, batchLength);
            memo.numBatches = Math.max(memo.numBatches, columns[i].unshift(child.slice(batchLength, length - batchLength)));
          }
        } else {
          const field = fields[i];
          fields[i] = field.clone({ nullable: true });
          children[i] = (_a = child === null || child === void 0 ? void 0 : child._changeLengthAndBackfillNullBitmap(batchLength)) !== null && _a !== void 0 ? _a : (0, data_js_1.makeData)({
            type: field.type,
            length: batchLength,
            nullCount: batchLength,
            nullBitmap: new Uint8Array(nullBitmapSize)
          });
        }
      }
      return children;
    }
  }
});

// node_modules/apache-arrow/table.js
var require_table = __commonJS({
  "node_modules/apache-arrow/table.js"(exports2) {
    "use strict";
    var _a;
    Object.defineProperty(exports2, "__esModule", { value: true });
    exports2.tableFromArrays = exports2.makeTable = exports2.Table = void 0;
    var enum_js_1 = require_enum();
    var data_js_1 = require_data();
    var factories_js_1 = require_factories();
    var vector_js_1 = require_vector2();
    var schema_js_1 = require_schema2();
    var type_js_1 = require_type2();
    var typecomparator_js_1 = require_typecomparator();
    var recordbatch_js_1 = require_recordbatch();
    var chunk_js_1 = require_chunk();
    var get_js_1 = require_get();
    var set_js_1 = require_set();
    var indexof_js_1 = require_indexof();
    var iterator_js_1 = require_iterator();
    var vector_js_2 = require_vector();
    var recordbatch_js_2 = require_recordbatch2();
    var Table = class _Table {
      constructor(...args) {
        var _b, _c;
        if (args.length === 0) {
          this.batches = [];
          this.schema = new schema_js_1.Schema([]);
          this._offsets = [0];
          return this;
        }
        let schema;
        let offsets;
        if (args[0] instanceof schema_js_1.Schema) {
          schema = args.shift();
        }
        if (args.at(-1) instanceof Uint32Array) {
          offsets = args.pop();
        }
        const unwrap = (x) => {
          if (x) {
            if (x instanceof recordbatch_js_2.RecordBatch) {
              return [x];
            } else if (x instanceof _Table) {
              return x.batches;
            } else if (x instanceof data_js_1.Data) {
              if (x.type instanceof type_js_1.Struct) {
                return [new recordbatch_js_2.RecordBatch(new schema_js_1.Schema(x.type.children), x)];
              }
            } else if (Array.isArray(x)) {
              return x.flatMap((v) => unwrap(v));
            } else if (typeof x[Symbol.iterator] === "function") {
              return [...x].flatMap((v) => unwrap(v));
            } else if (typeof x === "object") {
              const keys = Object.keys(x);
              const vecs = keys.map((k) => new vector_js_1.Vector([x[k]]));
              const batchSchema = schema !== null && schema !== void 0 ? schema : new schema_js_1.Schema(keys.map((k, i) => new schema_js_1.Field(String(k), vecs[i].type, vecs[i].nullable)));
              const [, batches2] = (0, recordbatch_js_1.distributeVectorsIntoRecordBatches)(batchSchema, vecs);
              return batches2.length === 0 ? [new recordbatch_js_2.RecordBatch(x)] : batches2;
            }
          }
          return [];
        };
        const batches = args.flatMap((v) => unwrap(v));
        schema = (_c = schema !== null && schema !== void 0 ? schema : (_b = batches[0]) === null || _b === void 0 ? void 0 : _b.schema) !== null && _c !== void 0 ? _c : new schema_js_1.Schema([]);
        if (!(schema instanceof schema_js_1.Schema)) {
          throw new TypeError("Table constructor expects a [Schema, RecordBatch[]] pair.");
        }
        for (const batch of batches) {
          if (!(batch instanceof recordbatch_js_2.RecordBatch)) {
            throw new TypeError("Table constructor expects a [Schema, RecordBatch[]] pair.");
          }
          if (!(0, typecomparator_js_1.compareSchemas)(schema, batch.schema)) {
            throw new TypeError("Table and inner RecordBatch schemas must be equivalent.");
          }
        }
        this.schema = schema;
        this.batches = batches;
        this._offsets = offsets !== null && offsets !== void 0 ? offsets : (0, chunk_js_1.computeChunkOffsets)(this.data);
      }
      /**
       * The contiguous {@link RecordBatch `RecordBatch`} chunks of the Table rows.
       */
      get data() {
        return this.batches.map(({ data }) => data);
      }
      /**
       * The number of columns in this Table.
       */
      get numCols() {
        return this.schema.fields.length;
      }
      /**
       * The number of rows in this Table.
       */
      get numRows() {
        return this.data.reduce((numRows, data) => numRows + data.length, 0);
      }
      /**
       * The number of null rows in this Table.
       */
      get nullCount() {
        if (this._nullCount === -1) {
          this._nullCount = (0, chunk_js_1.computeChunkNullCounts)(this.data);
        }
        return this._nullCount;
      }
      /**
       * Check whether an element is null.
       *
       * @param index The index at which to read the validity bitmap.
       */
      // @ts-ignore
      isValid(index) {
        return false;
      }
      /**
       * Get an element value by position.
       *
       * @param index The index of the element to read.
       */
      // @ts-ignore
      get(index) {
        return null;
      }
      /**
        * Get an element value by position.
        * @param index The index of the element to read. A negative index will count back from the last element.
        */
      // @ts-ignore
      at(index) {
        return this.get((0, vector_js_2.wrapIndex)(index, this.numRows));
      }
      /**
       * Set an element value by position.
       *
       * @param index The index of the element to write.
       * @param value The value to set.
       */
      // @ts-ignore
      set(index, value) {
        return;
      }
      /**
       * Retrieve the index of the first occurrence of a value in an Vector.
       *
       * @param element The value to locate in the Vector.
       * @param offset The index at which to begin the search. If offset is omitted, the search starts at index 0.
       */
      // @ts-ignore
      indexOf(element, offset) {
        return -1;
      }
      /**
       * Iterator for rows in this Table.
       */
      [Symbol.iterator]() {
        if (this.batches.length > 0) {
          return iterator_js_1.instance.visit(new vector_js_1.Vector(this.data));
        }
        return new Array(0)[Symbol.iterator]();
      }
      /**
       * Return a JavaScript Array of the Table rows.
       *
       * @returns An Array of Table rows.
       */
      toArray() {
        return [...this];
      }
      /**
       * Returns a string representation of the Table rows.
       *
       * @returns A string representation of the Table rows.
       */
      toString() {
        return `[
  ${this.toArray().join(",\n  ")}
]`;
      }
      /**
       * Combines two or more Tables of the same schema.
       *
       * @param others Additional Tables to add to the end of this Tables.
       */
      concat(...others) {
        const schema = this.schema;
        const data = this.data.concat(others.flatMap(({ data: data2 }) => data2));
        return new _Table(schema, data.map((data2) => new recordbatch_js_2.RecordBatch(schema, data2)));
      }
      /**
       * Return a zero-copy sub-section of this Table.
       *
       * @param begin The beginning of the specified portion of the Table.
       * @param end The end of the specified portion of the Table. This is exclusive of the element at the index 'end'.
       */
      slice(begin, end) {
        const schema = this.schema;
        [begin, end] = (0, vector_js_2.clampRange)({ length: this.numRows }, begin, end);
        const data = (0, chunk_js_1.sliceChunks)(this.data, this._offsets, begin, end);
        return new _Table(schema, data.map((chunk) => new recordbatch_js_2.RecordBatch(schema, chunk)));
      }
      /**
       * Returns a child Vector by name, or null if this Vector has no child with the given name.
       *
       * @param name The name of the child to retrieve.
       */
      getChild(name) {
        return this.getChildAt(this.schema.fields.findIndex((f) => f.name === name));
      }
      /**
       * Returns a child Vector by index, or null if this Vector has no child at the supplied index.
       *
       * @param index The index of the child to retrieve.
       */
      getChildAt(index) {
        if (index > -1 && index < this.schema.fields.length) {
          const data = this.data.map((data2) => data2.children[index]);
          if (data.length === 0) {
            const { type } = this.schema.fields[index];
            const empty = (0, data_js_1.makeData)({ type, length: 0, nullCount: 0 });
            data.push(empty._changeLengthAndBackfillNullBitmap(this.numRows));
          }
          return new vector_js_1.Vector(data);
        }
        return null;
      }
      /**
       * Sets a child Vector by name.
       *
       * @param name The name of the child to overwrite.
       * @returns A new Table with the supplied child for the specified name.
       */
      setChild(name, child) {
        var _b;
        return this.setChildAt((_b = this.schema.fields) === null || _b === void 0 ? void 0 : _b.findIndex((f) => f.name === name), child);
      }
      setChildAt(index, child) {
        let schema = this.schema;
        let batches = [...this.batches];
        if (index > -1 && index < this.numCols) {
          if (!child) {
            child = new vector_js_1.Vector([(0, data_js_1.makeData)({ type: new type_js_1.Null(), length: this.numRows })]);
          }
          const fields = schema.fields.slice();
          const field = fields[index].clone({ type: child.type });
          const children = this.schema.fields.map((_, i) => this.getChildAt(i));
          [fields[index], children[index]] = [field, child];
          [schema, batches] = (0, recordbatch_js_1.distributeVectorsIntoRecordBatches)(schema, children);
        }
        return new _Table(schema, batches);
      }
      /**
       * Construct a new Table containing only specified columns.
       *
       * @param columnNames Names of columns to keep.
       * @returns A new Table of columns matching the specified names.
       */
      select(columnNames) {
        const nameToIndex = this.schema.fields.reduce((m, f, i) => m.set(f.name, i), /* @__PURE__ */ new Map());
        return this.selectAt(columnNames.map((columnName) => nameToIndex.get(columnName)).filter((x) => x > -1));
      }
      /**
       * Construct a new Table containing only columns at the specified indices.
       *
       * @param columnIndices Indices of columns to keep.
       * @returns A new Table of columns at the specified indices.
       */
      selectAt(columnIndices) {
        const schema = this.schema.selectAt(columnIndices);
        const data = this.batches.map((batch) => batch.selectAt(columnIndices));
        return new _Table(schema, data);
      }
      assign(other) {
        const fields = this.schema.fields;
        const [indices, oldToNew] = other.schema.fields.reduce((memo, f2, newIdx) => {
          const [indices2, oldToNew2] = memo;
          const i = fields.findIndex((f) => f.name === f2.name);
          ~i ? oldToNew2[i] = newIdx : indices2.push(newIdx);
          return memo;
        }, [[], []]);
        const schema = this.schema.assign(other.schema);
        const columns = [
          ...fields.map((_, i) => [i, oldToNew[i]]).map(([i, j]) => j === void 0 ? this.getChildAt(i) : other.getChildAt(j)),
          ...indices.map((i) => other.getChildAt(i))
        ].filter(Boolean);
        return new _Table(...(0, recordbatch_js_1.distributeVectorsIntoRecordBatches)(schema, columns));
      }
    };
    exports2.Table = Table;
    _a = Symbol.toStringTag;
    Table[_a] = ((proto) => {
      proto.schema = null;
      proto.batches = [];
      proto._offsets = new Uint32Array([0]);
      proto._nullCount = -1;
      proto[Symbol.isConcatSpreadable] = true;
      proto["isValid"] = (0, chunk_js_1.wrapChunkedCall1)(chunk_js_1.isChunkedValid);
      proto["get"] = (0, chunk_js_1.wrapChunkedCall1)(get_js_1.instance.getVisitFn(enum_js_1.Type.Struct));
      proto["set"] = (0, chunk_js_1.wrapChunkedCall2)(set_js_1.instance.getVisitFn(enum_js_1.Type.Struct));
      proto["indexOf"] = (0, chunk_js_1.wrapChunkedIndexOf)(indexof_js_1.instance.getVisitFn(enum_js_1.Type.Struct));
      return "Table";
    })(Table.prototype);
    function makeTable(input) {
      const vecs = {};
      const inputs = Object.entries(input);
      for (const [key, col] of inputs) {
        vecs[key] = (0, vector_js_1.makeVector)(col);
      }
      return new Table(vecs);
    }
    exports2.makeTable = makeTable;
    function tableFromArrays(input) {
      const vecs = {};
      const inputs = Object.entries(input);
      for (const [key, col] of inputs) {
        vecs[key] = (0, factories_js_1.vectorFromArray)(col);
      }
      return new Table(vecs);
    }
    exports2.tableFromArrays = tableFromArrays;
  }
});

// node_modules/apache-arrow/recordbatch.js
var require_recordbatch2 = __commonJS({
  "node_modules/apache-arrow/recordbatch.js"(exports2) {
    "use strict";
    var _a;
    Object.defineProperty(exports2, "__esModule", { value: true });
    exports2._InternalEmptyPlaceholderRecordBatch = exports2.RecordBatch = void 0;
    var data_js_1 = require_data();
    var table_js_1 = require_table();
    var vector_js_1 = require_vector2();
    var schema_js_1 = require_schema2();
    var type_js_1 = require_type2();
    var vector_js_2 = require_vector();
    var get_js_1 = require_get();
    var set_js_1 = require_set();
    var indexof_js_1 = require_indexof();
    var iterator_js_1 = require_iterator();
    var RecordBatch = class _RecordBatch {
      constructor(...args) {
        switch (args.length) {
          case 2: {
            [this.schema] = args;
            if (!(this.schema instanceof schema_js_1.Schema)) {
              throw new TypeError("RecordBatch constructor expects a [Schema, Data] pair.");
            }
            [
              ,
              this.data = (0, data_js_1.makeData)({
                nullCount: 0,
                type: new type_js_1.Struct(this.schema.fields),
                children: this.schema.fields.map((f) => (0, data_js_1.makeData)({ type: f.type, nullCount: 0 }))
              })
            ] = args;
            if (!(this.data instanceof data_js_1.Data)) {
              throw new TypeError("RecordBatch constructor expects a [Schema, Data] pair.");
            }
            [this.schema, this.data] = ensureSameLengthData(this.schema, this.data.children);
            break;
          }
          case 1: {
            const [obj] = args;
            const { fields, children, length } = Object.keys(obj).reduce((memo, name, i) => {
              memo.children[i] = obj[name];
              memo.length = Math.max(memo.length, obj[name].length);
              memo.fields[i] = schema_js_1.Field.new({ name, type: obj[name].type, nullable: true });
              return memo;
            }, {
              length: 0,
              fields: new Array(),
              children: new Array()
            });
            const schema = new schema_js_1.Schema(fields);
            const data = (0, data_js_1.makeData)({ type: new type_js_1.Struct(fields), length, children, nullCount: 0 });
            [this.schema, this.data] = ensureSameLengthData(schema, data.children, length);
            break;
          }
          default:
            throw new TypeError("RecordBatch constructor expects an Object mapping names to child Data, or a [Schema, Data] pair.");
        }
      }
      get dictionaries() {
        return this._dictionaries || (this._dictionaries = collectDictionaries(this.schema.fields, this.data.children));
      }
      /**
       * The number of columns in this RecordBatch.
       */
      get numCols() {
        return this.schema.fields.length;
      }
      /**
       * The number of rows in this RecordBatch.
       */
      get numRows() {
        return this.data.length;
      }
      /**
       * The number of null rows in this RecordBatch.
       */
      get nullCount() {
        return this.data.nullCount;
      }
      /**
       * Check whether an row is null.
       * @param index The index at which to read the validity bitmap.
       */
      isValid(index) {
        return this.data.getValid(index);
      }
      /**
       * Get a row by position.
       * @param index The index of the row to read.
       */
      get(index) {
        return get_js_1.instance.visit(this.data, index);
      }
      /**
        * Get a row value by position.
        * @param index The index of the row to read. A negative index will count back from the last row.
        */
      at(index) {
        return this.get((0, vector_js_2.wrapIndex)(index, this.numRows));
      }
      /**
       * Set a row by position.
       * @param index The index of the row to write.
       * @param value The value to set.
       */
      set(index, value) {
        return set_js_1.instance.visit(this.data, index, value);
      }
      /**
       * Retrieve the index of the first occurrence of a row in an RecordBatch.
       * @param element The row to locate in the RecordBatch.
       * @param offset The index at which to begin the search. If offset is omitted, the search starts at index 0.
       */
      indexOf(element, offset) {
        return indexof_js_1.instance.visit(this.data, element, offset);
      }
      /**
       * Iterator for rows in this RecordBatch.
       */
      [Symbol.iterator]() {
        return iterator_js_1.instance.visit(new vector_js_1.Vector([this.data]));
      }
      /**
       * Return a JavaScript Array of the RecordBatch rows.
       * @returns An Array of RecordBatch rows.
       */
      toArray() {
        return [...this];
      }
      /**
       * Combines two or more RecordBatch of the same schema.
       * @param others Additional RecordBatch to add to the end of this RecordBatch.
       */
      concat(...others) {
        return new table_js_1.Table(this.schema, [this, ...others]);
      }
      /**
       * Return a zero-copy sub-section of this RecordBatch.
       * @param start The beginning of the specified portion of the RecordBatch.
       * @param end The end of the specified portion of the RecordBatch. This is exclusive of the row at the index 'end'.
       */
      slice(begin, end) {
        const [slice] = new vector_js_1.Vector([this.data]).slice(begin, end).data;
        return new _RecordBatch(this.schema, slice);
      }
      /**
       * Returns a child Vector by name, or null if this Vector has no child with the given name.
       * @param name The name of the child to retrieve.
       */
      getChild(name) {
        var _b;
        return this.getChildAt((_b = this.schema.fields) === null || _b === void 0 ? void 0 : _b.findIndex((f) => f.name === name));
      }
      /**
       * Returns a child Vector by index, or null if this Vector has no child at the supplied index.
       * @param index The index of the child to retrieve.
       */
      getChildAt(index) {
        if (index > -1 && index < this.schema.fields.length) {
          return new vector_js_1.Vector([this.data.children[index]]);
        }
        return null;
      }
      /**
       * Sets a child Vector by name.
       * @param name The name of the child to overwrite.
       * @returns A new RecordBatch with the new child for the specified name.
       */
      setChild(name, child) {
        var _b;
        return this.setChildAt((_b = this.schema.fields) === null || _b === void 0 ? void 0 : _b.findIndex((f) => f.name === name), child);
      }
      setChildAt(index, child) {
        let schema = this.schema;
        let data = this.data;
        if (index > -1 && index < this.numCols) {
          if (!child) {
            child = new vector_js_1.Vector([(0, data_js_1.makeData)({ type: new type_js_1.Null(), length: this.numRows })]);
          }
          const fields = schema.fields.slice();
          const children = data.children.slice();
          const field = fields[index].clone({ type: child.type });
          [fields[index], children[index]] = [field, child.data[0]];
          schema = new schema_js_1.Schema(fields, new Map(this.schema.metadata));
          data = (0, data_js_1.makeData)({ type: new type_js_1.Struct(fields), children });
        }
        return new _RecordBatch(schema, data);
      }
      /**
       * Construct a new RecordBatch containing only specified columns.
       *
       * @param columnNames Names of columns to keep.
       * @returns A new RecordBatch of columns matching the specified names.
       */
      select(columnNames) {
        const schema = this.schema.select(columnNames);
        const type = new type_js_1.Struct(schema.fields);
        const children = [];
        for (const name of columnNames) {
          const index = this.schema.fields.findIndex((f) => f.name === name);
          if (~index) {
            children[index] = this.data.children[index];
          }
        }
        return new _RecordBatch(schema, (0, data_js_1.makeData)({ type, length: this.numRows, children }));
      }
      /**
       * Construct a new RecordBatch containing only columns at the specified indices.
       *
       * @param columnIndices Indices of columns to keep.
       * @returns A new RecordBatch of columns matching at the specified indices.
       */
      selectAt(columnIndices) {
        const schema = this.schema.selectAt(columnIndices);
        const children = columnIndices.map((i) => this.data.children[i]).filter(Boolean);
        const subset = (0, data_js_1.makeData)({ type: new type_js_1.Struct(schema.fields), length: this.numRows, children });
        return new _RecordBatch(schema, subset);
      }
    };
    exports2.RecordBatch = RecordBatch;
    _a = Symbol.toStringTag;
    RecordBatch[_a] = ((proto) => {
      proto._nullCount = -1;
      proto[Symbol.isConcatSpreadable] = true;
      return "RecordBatch";
    })(RecordBatch.prototype);
    function ensureSameLengthData(schema, chunks, maxLength = chunks.reduce((max, col) => Math.max(max, col.length), 0)) {
      var _b;
      const fields = [...schema.fields];
      const children = [...chunks];
      const nullBitmapSize = (maxLength + 63 & ~63) >> 3;
      for (const [idx, field] of schema.fields.entries()) {
        const chunk = chunks[idx];
        if (!chunk || chunk.length !== maxLength) {
          fields[idx] = field.clone({ nullable: true });
          children[idx] = (_b = chunk === null || chunk === void 0 ? void 0 : chunk._changeLengthAndBackfillNullBitmap(maxLength)) !== null && _b !== void 0 ? _b : (0, data_js_1.makeData)({
            type: field.type,
            length: maxLength,
            nullCount: maxLength,
            nullBitmap: new Uint8Array(nullBitmapSize)
          });
        }
      }
      return [
        schema.assign(fields),
        (0, data_js_1.makeData)({ type: new type_js_1.Struct(fields), length: maxLength, children })
      ];
    }
    function collectDictionaries(fields, children, dictionaries = /* @__PURE__ */ new Map()) {
      var _b, _c;
      if (((_b = fields === null || fields === void 0 ? void 0 : fields.length) !== null && _b !== void 0 ? _b : 0) > 0 && (fields === null || fields === void 0 ? void 0 : fields.length) === (children === null || children === void 0 ? void 0 : children.length)) {
        for (let i = -1, n = fields.length; ++i < n; ) {
          const { type } = fields[i];
          const data = children[i];
          for (const next of [data, ...((_c = data === null || data === void 0 ? void 0 : data.dictionary) === null || _c === void 0 ? void 0 : _c.data) || []]) {
            collectDictionaries(type.children, next === null || next === void 0 ? void 0 : next.children, dictionaries);
          }
          if (type_js_1.DataType.isDictionary(type)) {
            const { id } = type;
            if (!dictionaries.has(id)) {
              if (data === null || data === void 0 ? void 0 : data.dictionary) {
                dictionaries.set(id, data.dictionary);
              }
            } else if (dictionaries.get(id) !== data.dictionary) {
              throw new Error(`Cannot create Schema containing two different dictionaries with the same Id`);
            }
          }
        }
      }
      return dictionaries;
    }
    var _InternalEmptyPlaceholderRecordBatch = class extends RecordBatch {
      constructor(schema) {
        const children = schema.fields.map((f) => (0, data_js_1.makeData)({ type: f.type }));
        const data = (0, data_js_1.makeData)({ type: new type_js_1.Struct(schema.fields), nullCount: 0, children });
        super(schema, data);
      }
    };
    exports2._InternalEmptyPlaceholderRecordBatch = _InternalEmptyPlaceholderRecordBatch;
  }
});

// node_modules/apache-arrow/fb/message.js
var require_message = __commonJS({
  "node_modules/apache-arrow/fb/message.js"(exports2) {
    "use strict";
    Object.defineProperty(exports2, "__esModule", { value: true });
    exports2.Message = void 0;
    var flatbuffers = (init_flatbuffers(), __toCommonJS(flatbuffers_exports));
    var key_value_js_1 = require_key_value();
    var message_header_js_1 = require_message_header();
    var metadata_version_js_1 = require_metadata_version();
    var Message = class _Message {
      constructor() {
        this.bb = null;
        this.bb_pos = 0;
      }
      __init(i, bb) {
        this.bb_pos = i;
        this.bb = bb;
        return this;
      }
      static getRootAsMessage(bb, obj) {
        return (obj || new _Message()).__init(bb.readInt32(bb.position()) + bb.position(), bb);
      }
      static getSizePrefixedRootAsMessage(bb, obj) {
        bb.setPosition(bb.position() + flatbuffers.SIZE_PREFIX_LENGTH);
        return (obj || new _Message()).__init(bb.readInt32(bb.position()) + bb.position(), bb);
      }
      version() {
        const offset = this.bb.__offset(this.bb_pos, 4);
        return offset ? this.bb.readInt16(this.bb_pos + offset) : metadata_version_js_1.MetadataVersion.V1;
      }
      headerType() {
        const offset = this.bb.__offset(this.bb_pos, 6);
        return offset ? this.bb.readUint8(this.bb_pos + offset) : message_header_js_1.MessageHeader.NONE;
      }
      header(obj) {
        const offset = this.bb.__offset(this.bb_pos, 8);
        return offset ? this.bb.__union(obj, this.bb_pos + offset) : null;
      }
      bodyLength() {
        const offset = this.bb.__offset(this.bb_pos, 10);
        return offset ? this.bb.readInt64(this.bb_pos + offset) : BigInt("0");
      }
      customMetadata(index, obj) {
        const offset = this.bb.__offset(this.bb_pos, 12);
        return offset ? (obj || new key_value_js_1.KeyValue()).__init(this.bb.__indirect(this.bb.__vector(this.bb_pos + offset) + index * 4), this.bb) : null;
      }
      customMetadataLength() {
        const offset = this.bb.__offset(this.bb_pos, 12);
        return offset ? this.bb.__vector_len(this.bb_pos + offset) : 0;
      }
      static startMessage(builder) {
        builder.startObject(5);
      }
      static addVersion(builder, version) {
        builder.addFieldInt16(0, version, metadata_version_js_1.MetadataVersion.V1);
      }
      static addHeaderType(builder, headerType) {
        builder.addFieldInt8(1, headerType, message_header_js_1.MessageHeader.NONE);
      }
      static addHeader(builder, headerOffset) {
        builder.addFieldOffset(2, headerOffset, 0);
      }
      static addBodyLength(builder, bodyLength) {
        builder.addFieldInt64(3, bodyLength, BigInt("0"));
      }
      static addCustomMetadata(builder, customMetadataOffset) {
        builder.addFieldOffset(4, customMetadataOffset, 0);
      }
      static createCustomMetadataVector(builder, data) {
        builder.startVector(4, data.length, 4);
        for (let i = data.length - 1; i >= 0; i--) {
          builder.addOffset(data[i]);
        }
        return builder.endVector();
      }
      static startCustomMetadataVector(builder, numElems) {
        builder.startVector(4, numElems, 4);
      }
      static endMessage(builder) {
        const offset = builder.endObject();
        return offset;
      }
      static finishMessageBuffer(builder, offset) {
        builder.finish(offset);
      }
      static finishSizePrefixedMessageBuffer(builder, offset) {
        builder.finish(offset, void 0, true);
      }
      static createMessage(builder, version, headerType, headerOffset, bodyLength, customMetadataOffset) {
        _Message.startMessage(builder);
        _Message.addVersion(builder, version);
        _Message.addHeaderType(builder, headerType);
        _Message.addHeader(builder, headerOffset);
        _Message.addBodyLength(builder, bodyLength);
        _Message.addCustomMetadata(builder, customMetadataOffset);
        return _Message.endMessage(builder);
      }
    };
    exports2.Message = Message;
  }
});

// node_modules/apache-arrow/visitor/typeassembler.js
var require_typeassembler = __commonJS({
  "node_modules/apache-arrow/visitor/typeassembler.js"(exports2) {
    "use strict";
    Object.defineProperty(exports2, "__esModule", { value: true });
    exports2.instance = exports2.TypeAssembler = void 0;
    var visitor_js_1 = require_visitor();
    var null_js_1 = require_null();
    var int_js_1 = require_int();
    var floating_point_js_1 = require_floating_point();
    var binary_js_1 = require_binary();
    var large_binary_js_1 = require_large_binary();
    var bool_js_1 = require_bool();
    var utf8_js_1 = require_utf82();
    var large_utf8_js_1 = require_large_utf8();
    var decimal_js_1 = require_decimal();
    var date_js_1 = require_date();
    var time_js_1 = require_time();
    var timestamp_js_1 = require_timestamp();
    var interval_js_1 = require_interval();
    var duration_js_1 = require_duration();
    var list_js_1 = require_list();
    var struct__js_1 = require_struct();
    var union_js_1 = require_union();
    var dictionary_encoding_js_1 = require_dictionary_encoding();
    var fixed_size_binary_js_1 = require_fixed_size_binary();
    var fixed_size_list_js_1 = require_fixed_size_list();
    var map_js_1 = require_map();
    var TypeAssembler = class extends visitor_js_1.Visitor {
      visit(node, builder) {
        return node == null || builder == null ? void 0 : super.visit(node, builder);
      }
      visitNull(_node, b) {
        null_js_1.Null.startNull(b);
        return null_js_1.Null.endNull(b);
      }
      visitInt(node, b) {
        int_js_1.Int.startInt(b);
        int_js_1.Int.addBitWidth(b, node.bitWidth);
        int_js_1.Int.addIsSigned(b, node.isSigned);
        return int_js_1.Int.endInt(b);
      }
      visitFloat(node, b) {
        floating_point_js_1.FloatingPoint.startFloatingPoint(b);
        floating_point_js_1.FloatingPoint.addPrecision(b, node.precision);
        return floating_point_js_1.FloatingPoint.endFloatingPoint(b);
      }
      visitBinary(_node, b) {
        binary_js_1.Binary.startBinary(b);
        return binary_js_1.Binary.endBinary(b);
      }
      visitLargeBinary(_node, b) {
        large_binary_js_1.LargeBinary.startLargeBinary(b);
        return large_binary_js_1.LargeBinary.endLargeBinary(b);
      }
      visitBool(_node, b) {
        bool_js_1.Bool.startBool(b);
        return bool_js_1.Bool.endBool(b);
      }
      visitUtf8(_node, b) {
        utf8_js_1.Utf8.startUtf8(b);
        return utf8_js_1.Utf8.endUtf8(b);
      }
      visitLargeUtf8(_node, b) {
        large_utf8_js_1.LargeUtf8.startLargeUtf8(b);
        return large_utf8_js_1.LargeUtf8.endLargeUtf8(b);
      }
      visitDecimal(node, b) {
        decimal_js_1.Decimal.startDecimal(b);
        decimal_js_1.Decimal.addScale(b, node.scale);
        decimal_js_1.Decimal.addPrecision(b, node.precision);
        decimal_js_1.Decimal.addBitWidth(b, node.bitWidth);
        return decimal_js_1.Decimal.endDecimal(b);
      }
      visitDate(node, b) {
        date_js_1.Date.startDate(b);
        date_js_1.Date.addUnit(b, node.unit);
        return date_js_1.Date.endDate(b);
      }
      visitTime(node, b) {
        time_js_1.Time.startTime(b);
        time_js_1.Time.addUnit(b, node.unit);
        time_js_1.Time.addBitWidth(b, node.bitWidth);
        return time_js_1.Time.endTime(b);
      }
      visitTimestamp(node, b) {
        const timezone = node.timezone && b.createString(node.timezone) || void 0;
        timestamp_js_1.Timestamp.startTimestamp(b);
        timestamp_js_1.Timestamp.addUnit(b, node.unit);
        if (timezone !== void 0) {
          timestamp_js_1.Timestamp.addTimezone(b, timezone);
        }
        return timestamp_js_1.Timestamp.endTimestamp(b);
      }
      visitInterval(node, b) {
        interval_js_1.Interval.startInterval(b);
        interval_js_1.Interval.addUnit(b, node.unit);
        return interval_js_1.Interval.endInterval(b);
      }
      visitDuration(node, b) {
        duration_js_1.Duration.startDuration(b);
        duration_js_1.Duration.addUnit(b, node.unit);
        return duration_js_1.Duration.endDuration(b);
      }
      visitList(_node, b) {
        list_js_1.List.startList(b);
        return list_js_1.List.endList(b);
      }
      visitStruct(_node, b) {
        struct__js_1.Struct_.startStruct_(b);
        return struct__js_1.Struct_.endStruct_(b);
      }
      visitUnion(node, b) {
        union_js_1.Union.startTypeIdsVector(b, node.typeIds.length);
        const typeIds = union_js_1.Union.createTypeIdsVector(b, node.typeIds);
        union_js_1.Union.startUnion(b);
        union_js_1.Union.addMode(b, node.mode);
        union_js_1.Union.addTypeIds(b, typeIds);
        return union_js_1.Union.endUnion(b);
      }
      visitDictionary(node, b) {
        const indexType = this.visit(node.indices, b);
        dictionary_encoding_js_1.DictionaryEncoding.startDictionaryEncoding(b);
        dictionary_encoding_js_1.DictionaryEncoding.addId(b, BigInt(node.id));
        dictionary_encoding_js_1.DictionaryEncoding.addIsOrdered(b, node.isOrdered);
        if (indexType !== void 0) {
          dictionary_encoding_js_1.DictionaryEncoding.addIndexType(b, indexType);
        }
        return dictionary_encoding_js_1.DictionaryEncoding.endDictionaryEncoding(b);
      }
      visitFixedSizeBinary(node, b) {
        fixed_size_binary_js_1.FixedSizeBinary.startFixedSizeBinary(b);
        fixed_size_binary_js_1.FixedSizeBinary.addByteWidth(b, node.byteWidth);
        return fixed_size_binary_js_1.FixedSizeBinary.endFixedSizeBinary(b);
      }
      visitFixedSizeList(node, b) {
        fixed_size_list_js_1.FixedSizeList.startFixedSizeList(b);
        fixed_size_list_js_1.FixedSizeList.addListSize(b, node.listSize);
        return fixed_size_list_js_1.FixedSizeList.endFixedSizeList(b);
      }
      visitMap(node, b) {
        map_js_1.Map.startMap(b);
        map_js_1.Map.addKeysSorted(b, node.keysSorted);
        return map_js_1.Map.endMap(b);
      }
    };
    exports2.TypeAssembler = TypeAssembler;
    exports2.instance = new TypeAssembler();
  }
});

// node_modules/apache-arrow/ipc/metadata/json.js
var require_json = __commonJS({
  "node_modules/apache-arrow/ipc/metadata/json.js"(exports2) {
    "use strict";
    Object.defineProperty(exports2, "__esModule", { value: true });
    exports2.fieldFromJSON = exports2.dictionaryBatchFromJSON = exports2.recordBatchFromJSON = exports2.schemaFromJSON = void 0;
    var schema_js_1 = require_schema2();
    var type_js_1 = require_type2();
    var message_js_1 = require_message2();
    var enum_js_1 = require_enum();
    function schemaFromJSON(_schema, dictionaries = /* @__PURE__ */ new Map()) {
      return new schema_js_1.Schema(schemaFieldsFromJSON(_schema, dictionaries), customMetadataFromJSON(_schema["metadata"]), dictionaries);
    }
    exports2.schemaFromJSON = schemaFromJSON;
    function recordBatchFromJSON(b) {
      return new message_js_1.RecordBatch(b["count"], fieldNodesFromJSON(b["columns"]), buffersFromJSON(b["columns"]));
    }
    exports2.recordBatchFromJSON = recordBatchFromJSON;
    function dictionaryBatchFromJSON(b) {
      return new message_js_1.DictionaryBatch(recordBatchFromJSON(b["data"]), b["id"], b["isDelta"]);
    }
    exports2.dictionaryBatchFromJSON = dictionaryBatchFromJSON;
    function schemaFieldsFromJSON(_schema, dictionaries) {
      return (_schema["fields"] || []).filter(Boolean).map((f) => schema_js_1.Field.fromJSON(f, dictionaries));
    }
    function fieldChildrenFromJSON(_field, dictionaries) {
      return (_field["children"] || []).filter(Boolean).map((f) => schema_js_1.Field.fromJSON(f, dictionaries));
    }
    function fieldNodesFromJSON(xs) {
      return (xs || []).reduce((fieldNodes, column) => [
        ...fieldNodes,
        new message_js_1.FieldNode(column["count"], nullCountFromJSON(column["VALIDITY"])),
        ...fieldNodesFromJSON(column["children"])
      ], []);
    }
    function buffersFromJSON(xs, buffers = []) {
      for (let i = -1, n = (xs || []).length; ++i < n; ) {
        const column = xs[i];
        column["VALIDITY"] && buffers.push(new message_js_1.BufferRegion(buffers.length, column["VALIDITY"].length));
        column["TYPE_ID"] && buffers.push(new message_js_1.BufferRegion(buffers.length, column["TYPE_ID"].length));
        column["OFFSET"] && buffers.push(new message_js_1.BufferRegion(buffers.length, column["OFFSET"].length));
        column["DATA"] && buffers.push(new message_js_1.BufferRegion(buffers.length, column["DATA"].length));
        buffers = buffersFromJSON(column["children"], buffers);
      }
      return buffers;
    }
    function nullCountFromJSON(validity) {
      return (validity || []).reduce((sum, val) => sum + +(val === 0), 0);
    }
    function fieldFromJSON(_field, dictionaries) {
      let id;
      let keys;
      let field;
      let dictMeta;
      let type;
      let dictType;
      if (!dictionaries || !(dictMeta = _field["dictionary"])) {
        type = typeFromJSON(_field, fieldChildrenFromJSON(_field, dictionaries));
        field = new schema_js_1.Field(_field["name"], type, _field["nullable"], customMetadataFromJSON(_field["metadata"]));
      } else if (!dictionaries.has(id = dictMeta["id"])) {
        keys = (keys = dictMeta["indexType"]) ? indexTypeFromJSON(keys) : new type_js_1.Int32();
        dictionaries.set(id, type = typeFromJSON(_field, fieldChildrenFromJSON(_field, dictionaries)));
        dictType = new type_js_1.Dictionary(type, keys, id, dictMeta["isOrdered"]);
        field = new schema_js_1.Field(_field["name"], dictType, _field["nullable"], customMetadataFromJSON(_field["metadata"]));
      } else {
        keys = (keys = dictMeta["indexType"]) ? indexTypeFromJSON(keys) : new type_js_1.Int32();
        dictType = new type_js_1.Dictionary(dictionaries.get(id), keys, id, dictMeta["isOrdered"]);
        field = new schema_js_1.Field(_field["name"], dictType, _field["nullable"], customMetadataFromJSON(_field["metadata"]));
      }
      return field || null;
    }
    exports2.fieldFromJSON = fieldFromJSON;
    function customMetadataFromJSON(metadata = []) {
      return new Map(metadata.map(({ key, value }) => [key, value]));
    }
    function indexTypeFromJSON(_type) {
      return new type_js_1.Int(_type["isSigned"], _type["bitWidth"]);
    }
    function typeFromJSON(f, children) {
      const typeId = f["type"]["name"];
      switch (typeId) {
        case "NONE":
          return new type_js_1.Null();
        case "null":
          return new type_js_1.Null();
        case "binary":
          return new type_js_1.Binary();
        case "largebinary":
          return new type_js_1.LargeBinary();
        case "utf8":
          return new type_js_1.Utf8();
        case "largeutf8":
          return new type_js_1.LargeUtf8();
        case "bool":
          return new type_js_1.Bool();
        case "list":
          return new type_js_1.List((children || [])[0]);
        case "struct":
          return new type_js_1.Struct(children || []);
        case "struct_":
          return new type_js_1.Struct(children || []);
      }
      switch (typeId) {
        case "int": {
          const t = f["type"];
          return new type_js_1.Int(t["isSigned"], t["bitWidth"]);
        }
        case "floatingpoint": {
          const t = f["type"];
          return new type_js_1.Float(enum_js_1.Precision[t["precision"]]);
        }
        case "decimal": {
          const t = f["type"];
          return new type_js_1.Decimal(t["scale"], t["precision"], t["bitWidth"]);
        }
        case "date": {
          const t = f["type"];
          return new type_js_1.Date_(enum_js_1.DateUnit[t["unit"]]);
        }
        case "time": {
          const t = f["type"];
          return new type_js_1.Time(enum_js_1.TimeUnit[t["unit"]], t["bitWidth"]);
        }
        case "timestamp": {
          const t = f["type"];
          return new type_js_1.Timestamp(enum_js_1.TimeUnit[t["unit"]], t["timezone"]);
        }
        case "interval": {
          const t = f["type"];
          return new type_js_1.Interval(enum_js_1.IntervalUnit[t["unit"]]);
        }
        case "duration": {
          const t = f["type"];
          return new type_js_1.Duration(enum_js_1.TimeUnit[t["unit"]]);
        }
        case "union": {
          const t = f["type"];
          const [m, ...ms] = (t["mode"] + "").toLowerCase();
          const mode = m.toUpperCase() + ms.join("");
          return new type_js_1.Union(enum_js_1.UnionMode[mode], t["typeIds"] || [], children || []);
        }
        case "fixedsizebinary": {
          const t = f["type"];
          return new type_js_1.FixedSizeBinary(t["byteWidth"]);
        }
        case "fixedsizelist": {
          const t = f["type"];
          return new type_js_1.FixedSizeList(t["listSize"], (children || [])[0]);
        }
        case "map": {
          const t = f["type"];
          return new type_js_1.Map_((children || [])[0], t["keysSorted"]);
        }
      }
      throw new Error(`Unrecognized type: "${typeId}"`);
    }
  }
});

// node_modules/apache-arrow/ipc/metadata/message.js
var require_message2 = __commonJS({
  "node_modules/apache-arrow/ipc/metadata/message.js"(exports2) {
    "use strict";
    Object.defineProperty(exports2, "__esModule", { value: true });
    exports2.FieldNode = exports2.BufferRegion = exports2.DictionaryBatch = exports2.RecordBatch = exports2.Message = void 0;
    var flatbuffers = (init_flatbuffers(), __toCommonJS(flatbuffers_exports));
    var schema_js_1 = require_schema();
    var int_js_1 = require_int();
    var record_batch_js_1 = require_record_batch();
    var dictionary_batch_js_1 = require_dictionary_batch();
    var buffer_js_1 = require_buffer2();
    var field_js_1 = require_field();
    var field_node_js_1 = require_field_node();
    var type_js_1 = require_type();
    var key_value_js_1 = require_key_value();
    var endianness_js_1 = require_endianness();
    var floating_point_js_1 = require_floating_point();
    var decimal_js_1 = require_decimal();
    var date_js_1 = require_date();
    var time_js_1 = require_time();
    var timestamp_js_1 = require_timestamp();
    var interval_js_1 = require_interval();
    var duration_js_1 = require_duration();
    var union_js_1 = require_union();
    var fixed_size_binary_js_1 = require_fixed_size_binary();
    var fixed_size_list_js_1 = require_fixed_size_list();
    var map_js_1 = require_map();
    var message_js_1 = require_message();
    var schema_js_2 = require_schema2();
    var buffer_js_2 = require_buffer();
    var bigint_js_1 = require_bigint();
    var enum_js_1 = require_enum();
    var typeassembler_js_1 = require_typeassembler();
    var json_js_1 = require_json();
    var Builder2 = flatbuffers.Builder;
    var ByteBuffer2 = flatbuffers.ByteBuffer;
    var type_js_2 = require_type2();
    var Message = class _Message {
      /** @nocollapse */
      static fromJSON(msg, headerType) {
        const message = new _Message(0, enum_js_1.MetadataVersion.V5, headerType);
        message._createHeader = messageHeaderFromJSON(msg, headerType);
        return message;
      }
      /** @nocollapse */
      static decode(buf) {
        buf = new ByteBuffer2((0, buffer_js_2.toUint8Array)(buf));
        const _message = message_js_1.Message.getRootAsMessage(buf);
        const bodyLength = _message.bodyLength();
        const version = _message.version();
        const headerType = _message.headerType();
        const message = new _Message(bodyLength, version, headerType);
        message._createHeader = decodeMessageHeader(_message, headerType);
        return message;
      }
      /** @nocollapse */
      static encode(message) {
        const b = new Builder2();
        let headerOffset = -1;
        if (message.isSchema()) {
          headerOffset = schema_js_2.Schema.encode(b, message.header());
        } else if (message.isRecordBatch()) {
          headerOffset = RecordBatch.encode(b, message.header());
        } else if (message.isDictionaryBatch()) {
          headerOffset = DictionaryBatch.encode(b, message.header());
        }
        message_js_1.Message.startMessage(b);
        message_js_1.Message.addVersion(b, enum_js_1.MetadataVersion.V5);
        message_js_1.Message.addHeader(b, headerOffset);
        message_js_1.Message.addHeaderType(b, message.headerType);
        message_js_1.Message.addBodyLength(b, BigInt(message.bodyLength));
        message_js_1.Message.finishMessageBuffer(b, message_js_1.Message.endMessage(b));
        return b.asUint8Array();
      }
      /** @nocollapse */
      static from(header, bodyLength = 0) {
        if (header instanceof schema_js_2.Schema) {
          return new _Message(0, enum_js_1.MetadataVersion.V5, enum_js_1.MessageHeader.Schema, header);
        }
        if (header instanceof RecordBatch) {
          return new _Message(bodyLength, enum_js_1.MetadataVersion.V5, enum_js_1.MessageHeader.RecordBatch, header);
        }
        if (header instanceof DictionaryBatch) {
          return new _Message(bodyLength, enum_js_1.MetadataVersion.V5, enum_js_1.MessageHeader.DictionaryBatch, header);
        }
        throw new Error(`Unrecognized Message header: ${header}`);
      }
      get type() {
        return this.headerType;
      }
      get version() {
        return this._version;
      }
      get headerType() {
        return this._headerType;
      }
      get bodyLength() {
        return this._bodyLength;
      }
      header() {
        return this._createHeader();
      }
      isSchema() {
        return this.headerType === enum_js_1.MessageHeader.Schema;
      }
      isRecordBatch() {
        return this.headerType === enum_js_1.MessageHeader.RecordBatch;
      }
      isDictionaryBatch() {
        return this.headerType === enum_js_1.MessageHeader.DictionaryBatch;
      }
      constructor(bodyLength, version, headerType, header) {
        this._version = version;
        this._headerType = headerType;
        this.body = new Uint8Array(0);
        header && (this._createHeader = () => header);
        this._bodyLength = (0, bigint_js_1.bigIntToNumber)(bodyLength);
      }
    };
    exports2.Message = Message;
    var RecordBatch = class {
      get nodes() {
        return this._nodes;
      }
      get length() {
        return this._length;
      }
      get buffers() {
        return this._buffers;
      }
      constructor(length, nodes, buffers) {
        this._nodes = nodes;
        this._buffers = buffers;
        this._length = (0, bigint_js_1.bigIntToNumber)(length);
      }
    };
    exports2.RecordBatch = RecordBatch;
    var DictionaryBatch = class {
      get id() {
        return this._id;
      }
      get data() {
        return this._data;
      }
      get isDelta() {
        return this._isDelta;
      }
      get length() {
        return this.data.length;
      }
      get nodes() {
        return this.data.nodes;
      }
      get buffers() {
        return this.data.buffers;
      }
      constructor(data, id, isDelta = false) {
        this._data = data;
        this._isDelta = isDelta;
        this._id = (0, bigint_js_1.bigIntToNumber)(id);
      }
    };
    exports2.DictionaryBatch = DictionaryBatch;
    var BufferRegion = class {
      constructor(offset, length) {
        this.offset = (0, bigint_js_1.bigIntToNumber)(offset);
        this.length = (0, bigint_js_1.bigIntToNumber)(length);
      }
    };
    exports2.BufferRegion = BufferRegion;
    var FieldNode = class {
      constructor(length, nullCount) {
        this.length = (0, bigint_js_1.bigIntToNumber)(length);
        this.nullCount = (0, bigint_js_1.bigIntToNumber)(nullCount);
      }
    };
    exports2.FieldNode = FieldNode;
    function messageHeaderFromJSON(message, type) {
      return (() => {
        switch (type) {
          case enum_js_1.MessageHeader.Schema:
            return schema_js_2.Schema.fromJSON(message);
          case enum_js_1.MessageHeader.RecordBatch:
            return RecordBatch.fromJSON(message);
          case enum_js_1.MessageHeader.DictionaryBatch:
            return DictionaryBatch.fromJSON(message);
        }
        throw new Error(`Unrecognized Message type: { name: ${enum_js_1.MessageHeader[type]}, type: ${type} }`);
      });
    }
    function decodeMessageHeader(message, type) {
      return (() => {
        switch (type) {
          case enum_js_1.MessageHeader.Schema:
            return schema_js_2.Schema.decode(message.header(new schema_js_1.Schema()), /* @__PURE__ */ new Map(), message.version());
          case enum_js_1.MessageHeader.RecordBatch:
            return RecordBatch.decode(message.header(new record_batch_js_1.RecordBatch()), message.version());
          case enum_js_1.MessageHeader.DictionaryBatch:
            return DictionaryBatch.decode(message.header(new dictionary_batch_js_1.DictionaryBatch()), message.version());
        }
        throw new Error(`Unrecognized Message type: { name: ${enum_js_1.MessageHeader[type]}, type: ${type} }`);
      });
    }
    schema_js_2.Field["encode"] = encodeField;
    schema_js_2.Field["decode"] = decodeField;
    schema_js_2.Field["fromJSON"] = json_js_1.fieldFromJSON;
    schema_js_2.Schema["encode"] = encodeSchema;
    schema_js_2.Schema["decode"] = decodeSchema;
    schema_js_2.Schema["fromJSON"] = json_js_1.schemaFromJSON;
    RecordBatch["encode"] = encodeRecordBatch;
    RecordBatch["decode"] = decodeRecordBatch;
    RecordBatch["fromJSON"] = json_js_1.recordBatchFromJSON;
    DictionaryBatch["encode"] = encodeDictionaryBatch;
    DictionaryBatch["decode"] = decodeDictionaryBatch;
    DictionaryBatch["fromJSON"] = json_js_1.dictionaryBatchFromJSON;
    FieldNode["encode"] = encodeFieldNode;
    FieldNode["decode"] = decodeFieldNode;
    BufferRegion["encode"] = encodeBufferRegion;
    BufferRegion["decode"] = decodeBufferRegion;
    function decodeSchema(_schema, dictionaries = /* @__PURE__ */ new Map(), version = enum_js_1.MetadataVersion.V5) {
      const fields = decodeSchemaFields(_schema, dictionaries);
      return new schema_js_2.Schema(fields, decodeCustomMetadata(_schema), dictionaries, version);
    }
    function decodeRecordBatch(batch, version = enum_js_1.MetadataVersion.V5) {
      if (batch.compression() !== null) {
        throw new Error("Record batch compression not implemented");
      }
      return new RecordBatch(batch.length(), decodeFieldNodes(batch), decodeBuffers(batch, version));
    }
    function decodeDictionaryBatch(batch, version = enum_js_1.MetadataVersion.V5) {
      return new DictionaryBatch(RecordBatch.decode(batch.data(), version), batch.id(), batch.isDelta());
    }
    function decodeBufferRegion(b) {
      return new BufferRegion(b.offset(), b.length());
    }
    function decodeFieldNode(f) {
      return new FieldNode(f.length(), f.nullCount());
    }
    function decodeFieldNodes(batch) {
      const nodes = [];
      for (let f, i = -1, j = -1, n = batch.nodesLength(); ++i < n; ) {
        if (f = batch.nodes(i)) {
          nodes[++j] = FieldNode.decode(f);
        }
      }
      return nodes;
    }
    function decodeBuffers(batch, version) {
      const bufferRegions = [];
      for (let b, i = -1, j = -1, n = batch.buffersLength(); ++i < n; ) {
        if (b = batch.buffers(i)) {
          if (version < enum_js_1.MetadataVersion.V4) {
            b.bb_pos += 8 * (i + 1);
          }
          bufferRegions[++j] = BufferRegion.decode(b);
        }
      }
      return bufferRegions;
    }
    function decodeSchemaFields(schema, dictionaries) {
      const fields = [];
      for (let f, i = -1, j = -1, n = schema.fieldsLength(); ++i < n; ) {
        if (f = schema.fields(i)) {
          fields[++j] = schema_js_2.Field.decode(f, dictionaries);
        }
      }
      return fields;
    }
    function decodeFieldChildren(field, dictionaries) {
      const children = [];
      for (let f, i = -1, j = -1, n = field.childrenLength(); ++i < n; ) {
        if (f = field.children(i)) {
          children[++j] = schema_js_2.Field.decode(f, dictionaries);
        }
      }
      return children;
    }
    function decodeField(f, dictionaries) {
      let id;
      let field;
      let type;
      let keys;
      let dictType;
      let dictMeta;
      if (!dictionaries || !(dictMeta = f.dictionary())) {
        type = decodeFieldType(f, decodeFieldChildren(f, dictionaries));
        field = new schema_js_2.Field(f.name(), type, f.nullable(), decodeCustomMetadata(f));
      } else if (!dictionaries.has(id = (0, bigint_js_1.bigIntToNumber)(dictMeta.id()))) {
        keys = (keys = dictMeta.indexType()) ? decodeIndexType(keys) : new type_js_2.Int32();
        dictionaries.set(id, type = decodeFieldType(f, decodeFieldChildren(f, dictionaries)));
        dictType = new type_js_2.Dictionary(type, keys, id, dictMeta.isOrdered());
        field = new schema_js_2.Field(f.name(), dictType, f.nullable(), decodeCustomMetadata(f));
      } else {
        keys = (keys = dictMeta.indexType()) ? decodeIndexType(keys) : new type_js_2.Int32();
        dictType = new type_js_2.Dictionary(dictionaries.get(id), keys, id, dictMeta.isOrdered());
        field = new schema_js_2.Field(f.name(), dictType, f.nullable(), decodeCustomMetadata(f));
      }
      return field || null;
    }
    function decodeCustomMetadata(parent) {
      const data = /* @__PURE__ */ new Map();
      if (parent) {
        for (let entry, key, i = -1, n = Math.trunc(parent.customMetadataLength()); ++i < n; ) {
          if ((entry = parent.customMetadata(i)) && (key = entry.key()) != null) {
            data.set(key, entry.value());
          }
        }
      }
      return data;
    }
    function decodeIndexType(_type) {
      return new type_js_2.Int(_type.isSigned(), _type.bitWidth());
    }
    function decodeFieldType(f, children) {
      const typeId = f.typeType();
      switch (typeId) {
        case type_js_1.Type["NONE"]:
          return new type_js_2.Null();
        case type_js_1.Type["Null"]:
          return new type_js_2.Null();
        case type_js_1.Type["Binary"]:
          return new type_js_2.Binary();
        case type_js_1.Type["LargeBinary"]:
          return new type_js_2.LargeBinary();
        case type_js_1.Type["Utf8"]:
          return new type_js_2.Utf8();
        case type_js_1.Type["LargeUtf8"]:
          return new type_js_2.LargeUtf8();
        case type_js_1.Type["Bool"]:
          return new type_js_2.Bool();
        case type_js_1.Type["List"]:
          return new type_js_2.List((children || [])[0]);
        case type_js_1.Type["Struct_"]:
          return new type_js_2.Struct(children || []);
      }
      switch (typeId) {
        case type_js_1.Type["Int"]: {
          const t = f.type(new int_js_1.Int());
          return new type_js_2.Int(t.isSigned(), t.bitWidth());
        }
        case type_js_1.Type["FloatingPoint"]: {
          const t = f.type(new floating_point_js_1.FloatingPoint());
          return new type_js_2.Float(t.precision());
        }
        case type_js_1.Type["Decimal"]: {
          const t = f.type(new decimal_js_1.Decimal());
          return new type_js_2.Decimal(t.scale(), t.precision(), t.bitWidth());
        }
        case type_js_1.Type["Date"]: {
          const t = f.type(new date_js_1.Date());
          return new type_js_2.Date_(t.unit());
        }
        case type_js_1.Type["Time"]: {
          const t = f.type(new time_js_1.Time());
          return new type_js_2.Time(t.unit(), t.bitWidth());
        }
        case type_js_1.Type["Timestamp"]: {
          const t = f.type(new timestamp_js_1.Timestamp());
          return new type_js_2.Timestamp(t.unit(), t.timezone());
        }
        case type_js_1.Type["Interval"]: {
          const t = f.type(new interval_js_1.Interval());
          return new type_js_2.Interval(t.unit());
        }
        case type_js_1.Type["Duration"]: {
          const t = f.type(new duration_js_1.Duration());
          return new type_js_2.Duration(t.unit());
        }
        case type_js_1.Type["Union"]: {
          const t = f.type(new union_js_1.Union());
          return new type_js_2.Union(t.mode(), t.typeIdsArray() || [], children || []);
        }
        case type_js_1.Type["FixedSizeBinary"]: {
          const t = f.type(new fixed_size_binary_js_1.FixedSizeBinary());
          return new type_js_2.FixedSizeBinary(t.byteWidth());
        }
        case type_js_1.Type["FixedSizeList"]: {
          const t = f.type(new fixed_size_list_js_1.FixedSizeList());
          return new type_js_2.FixedSizeList(t.listSize(), (children || [])[0]);
        }
        case type_js_1.Type["Map"]: {
          const t = f.type(new map_js_1.Map());
          return new type_js_2.Map_((children || [])[0], t.keysSorted());
        }
      }
      throw new Error(`Unrecognized type: "${type_js_1.Type[typeId]}" (${typeId})`);
    }
    function encodeSchema(b, schema) {
      const fieldOffsets = schema.fields.map((f) => schema_js_2.Field.encode(b, f));
      schema_js_1.Schema.startFieldsVector(b, fieldOffsets.length);
      const fieldsVectorOffset = schema_js_1.Schema.createFieldsVector(b, fieldOffsets);
      const metadataOffset = !(schema.metadata && schema.metadata.size > 0) ? -1 : schema_js_1.Schema.createCustomMetadataVector(b, [...schema.metadata].map(([k, v]) => {
        const key = b.createString(`${k}`);
        const val = b.createString(`${v}`);
        key_value_js_1.KeyValue.startKeyValue(b);
        key_value_js_1.KeyValue.addKey(b, key);
        key_value_js_1.KeyValue.addValue(b, val);
        return key_value_js_1.KeyValue.endKeyValue(b);
      }));
      schema_js_1.Schema.startSchema(b);
      schema_js_1.Schema.addFields(b, fieldsVectorOffset);
      schema_js_1.Schema.addEndianness(b, platformIsLittleEndian ? endianness_js_1.Endianness.Little : endianness_js_1.Endianness.Big);
      if (metadataOffset !== -1) {
        schema_js_1.Schema.addCustomMetadata(b, metadataOffset);
      }
      return schema_js_1.Schema.endSchema(b);
    }
    function encodeField(b, field) {
      let nameOffset = -1;
      let typeOffset = -1;
      let dictionaryOffset = -1;
      const type = field.type;
      let typeId = field.typeId;
      if (!type_js_2.DataType.isDictionary(type)) {
        typeOffset = typeassembler_js_1.instance.visit(type, b);
      } else {
        typeId = type.dictionary.typeId;
        dictionaryOffset = typeassembler_js_1.instance.visit(type, b);
        typeOffset = typeassembler_js_1.instance.visit(type.dictionary, b);
      }
      const childOffsets = (type.children || []).map((f) => schema_js_2.Field.encode(b, f));
      const childrenVectorOffset = field_js_1.Field.createChildrenVector(b, childOffsets);
      const metadataOffset = !(field.metadata && field.metadata.size > 0) ? -1 : field_js_1.Field.createCustomMetadataVector(b, [...field.metadata].map(([k, v]) => {
        const key = b.createString(`${k}`);
        const val = b.createString(`${v}`);
        key_value_js_1.KeyValue.startKeyValue(b);
        key_value_js_1.KeyValue.addKey(b, key);
        key_value_js_1.KeyValue.addValue(b, val);
        return key_value_js_1.KeyValue.endKeyValue(b);
      }));
      if (field.name) {
        nameOffset = b.createString(field.name);
      }
      field_js_1.Field.startField(b);
      field_js_1.Field.addType(b, typeOffset);
      field_js_1.Field.addTypeType(b, typeId);
      field_js_1.Field.addChildren(b, childrenVectorOffset);
      field_js_1.Field.addNullable(b, !!field.nullable);
      if (nameOffset !== -1) {
        field_js_1.Field.addName(b, nameOffset);
      }
      if (dictionaryOffset !== -1) {
        field_js_1.Field.addDictionary(b, dictionaryOffset);
      }
      if (metadataOffset !== -1) {
        field_js_1.Field.addCustomMetadata(b, metadataOffset);
      }
      return field_js_1.Field.endField(b);
    }
    function encodeRecordBatch(b, recordBatch) {
      const nodes = recordBatch.nodes || [];
      const buffers = recordBatch.buffers || [];
      record_batch_js_1.RecordBatch.startNodesVector(b, nodes.length);
      for (const n of nodes.slice().reverse())
        FieldNode.encode(b, n);
      const nodesVectorOffset = b.endVector();
      record_batch_js_1.RecordBatch.startBuffersVector(b, buffers.length);
      for (const b_ of buffers.slice().reverse())
        BufferRegion.encode(b, b_);
      const buffersVectorOffset = b.endVector();
      record_batch_js_1.RecordBatch.startRecordBatch(b);
      record_batch_js_1.RecordBatch.addLength(b, BigInt(recordBatch.length));
      record_batch_js_1.RecordBatch.addNodes(b, nodesVectorOffset);
      record_batch_js_1.RecordBatch.addBuffers(b, buffersVectorOffset);
      return record_batch_js_1.RecordBatch.endRecordBatch(b);
    }
    function encodeDictionaryBatch(b, dictionaryBatch) {
      const dataOffset = RecordBatch.encode(b, dictionaryBatch.data);
      dictionary_batch_js_1.DictionaryBatch.startDictionaryBatch(b);
      dictionary_batch_js_1.DictionaryBatch.addId(b, BigInt(dictionaryBatch.id));
      dictionary_batch_js_1.DictionaryBatch.addIsDelta(b, dictionaryBatch.isDelta);
      dictionary_batch_js_1.DictionaryBatch.addData(b, dataOffset);
      return dictionary_batch_js_1.DictionaryBatch.endDictionaryBatch(b);
    }
    function encodeFieldNode(b, node) {
      return field_node_js_1.FieldNode.createFieldNode(b, BigInt(node.length), BigInt(node.nullCount));
    }
    function encodeBufferRegion(b, node) {
      return buffer_js_1.Buffer.createBuffer(b, BigInt(node.offset), BigInt(node.length));
    }
    var platformIsLittleEndian = (() => {
      const buffer = new ArrayBuffer(2);
      new DataView(buffer).setInt16(
        0,
        256,
        true
        /* littleEndian */
      );
      return new Int16Array(buffer)[0] === 256;
    })();
  }
});

// node_modules/apache-arrow/ipc/message.js
var require_message3 = __commonJS({
  "node_modules/apache-arrow/ipc/message.js"(exports2) {
    "use strict";
    Object.defineProperty(exports2, "__esModule", { value: true });
    exports2.magicX2AndPadding = exports2.magicAndPadding = exports2.magicLength = exports2.checkForMagicArrowString = exports2.MAGIC = exports2.MAGIC_STR = exports2.PADDING = exports2.JSONMessageReader = exports2.AsyncMessageReader = exports2.MessageReader = void 0;
    var tslib_1 = (init_tslib_es6(), __toCommonJS(tslib_es6_exports));
    var enum_js_1 = require_enum();
    var flatbuffers_1 = (init_flatbuffers(), __toCommonJS(flatbuffers_exports));
    var message_js_1 = require_message2();
    var compat_js_1 = require_compat();
    var file_js_1 = require_file2();
    var buffer_js_1 = require_buffer();
    var stream_js_1 = require_stream();
    var interfaces_js_1 = require_interfaces();
    var invalidMessageType = (type) => `Expected ${enum_js_1.MessageHeader[type]} Message in stream, but was null or length 0.`;
    var nullMessage = (type) => `Header pointer of flatbuffer-encoded ${enum_js_1.MessageHeader[type]} Message is null or length 0.`;
    var invalidMessageMetadata = (expected, actual) => `Expected to read ${expected} metadata bytes, but only read ${actual}.`;
    var invalidMessageBodyLength = (expected, actual) => `Expected to read ${expected} bytes for message body, but only read ${actual}.`;
    var MessageReader = class {
      constructor(source) {
        this.source = source instanceof stream_js_1.ByteStream ? source : new stream_js_1.ByteStream(source);
      }
      [Symbol.iterator]() {
        return this;
      }
      next() {
        let r;
        if ((r = this.readMetadataLength()).done) {
          return interfaces_js_1.ITERATOR_DONE;
        }
        if (r.value === -1 && (r = this.readMetadataLength()).done) {
          return interfaces_js_1.ITERATOR_DONE;
        }
        if ((r = this.readMetadata(r.value)).done) {
          return interfaces_js_1.ITERATOR_DONE;
        }
        return r;
      }
      throw(value) {
        return this.source.throw(value);
      }
      return(value) {
        return this.source.return(value);
      }
      readMessage(type) {
        let r;
        if ((r = this.next()).done) {
          return null;
        }
        if (type != null && r.value.headerType !== type) {
          throw new Error(invalidMessageType(type));
        }
        return r.value;
      }
      readMessageBody(bodyLength) {
        if (bodyLength <= 0) {
          return new Uint8Array(0);
        }
        const buf = (0, buffer_js_1.toUint8Array)(this.source.read(bodyLength));
        if (buf.byteLength < bodyLength) {
          throw new Error(invalidMessageBodyLength(bodyLength, buf.byteLength));
        }
        return (
          /* 1. */
          buf.byteOffset % 8 === 0 && /* 2. */
          buf.byteOffset + buf.byteLength <= buf.buffer.byteLength ? buf : buf.slice()
        );
      }
      readSchema(throwIfNull = false) {
        const type = enum_js_1.MessageHeader.Schema;
        const message = this.readMessage(type);
        const schema = message === null || message === void 0 ? void 0 : message.header();
        if (throwIfNull && !schema) {
          throw new Error(nullMessage(type));
        }
        return schema;
      }
      readMetadataLength() {
        const buf = this.source.read(exports2.PADDING);
        const bb = buf && new flatbuffers_1.ByteBuffer(buf);
        const len = (bb === null || bb === void 0 ? void 0 : bb.readInt32(0)) || 0;
        return { done: len === 0, value: len };
      }
      readMetadata(metadataLength) {
        const buf = this.source.read(metadataLength);
        if (!buf) {
          return interfaces_js_1.ITERATOR_DONE;
        }
        if (buf.byteLength < metadataLength) {
          throw new Error(invalidMessageMetadata(metadataLength, buf.byteLength));
        }
        return { done: false, value: message_js_1.Message.decode(buf) };
      }
    };
    exports2.MessageReader = MessageReader;
    var AsyncMessageReader = class {
      constructor(source, byteLength) {
        this.source = source instanceof stream_js_1.AsyncByteStream ? source : (0, compat_js_1.isFileHandle)(source) ? new file_js_1.AsyncRandomAccessFile(source, byteLength) : new stream_js_1.AsyncByteStream(source);
      }
      [Symbol.asyncIterator]() {
        return this;
      }
      next() {
        return tslib_1.__awaiter(this, void 0, void 0, function* () {
          let r;
          if ((r = yield this.readMetadataLength()).done) {
            return interfaces_js_1.ITERATOR_DONE;
          }
          if (r.value === -1 && (r = yield this.readMetadataLength()).done) {
            return interfaces_js_1.ITERATOR_DONE;
          }
          if ((r = yield this.readMetadata(r.value)).done) {
            return interfaces_js_1.ITERATOR_DONE;
          }
          return r;
        });
      }
      throw(value) {
        return tslib_1.__awaiter(this, void 0, void 0, function* () {
          return yield this.source.throw(value);
        });
      }
      return(value) {
        return tslib_1.__awaiter(this, void 0, void 0, function* () {
          return yield this.source.return(value);
        });
      }
      readMessage(type) {
        return tslib_1.__awaiter(this, void 0, void 0, function* () {
          let r;
          if ((r = yield this.next()).done) {
            return null;
          }
          if (type != null && r.value.headerType !== type) {
            throw new Error(invalidMessageType(type));
          }
          return r.value;
        });
      }
      readMessageBody(bodyLength) {
        return tslib_1.__awaiter(this, void 0, void 0, function* () {
          if (bodyLength <= 0) {
            return new Uint8Array(0);
          }
          const buf = (0, buffer_js_1.toUint8Array)(yield this.source.read(bodyLength));
          if (buf.byteLength < bodyLength) {
            throw new Error(invalidMessageBodyLength(bodyLength, buf.byteLength));
          }
          return (
            /* 1. */
            buf.byteOffset % 8 === 0 && /* 2. */
            buf.byteOffset + buf.byteLength <= buf.buffer.byteLength ? buf : buf.slice()
          );
        });
      }
      readSchema() {
        return tslib_1.__awaiter(this, arguments, void 0, function* (throwIfNull = false) {
          const type = enum_js_1.MessageHeader.Schema;
          const message = yield this.readMessage(type);
          const schema = message === null || message === void 0 ? void 0 : message.header();
          if (throwIfNull && !schema) {
            throw new Error(nullMessage(type));
          }
          return schema;
        });
      }
      readMetadataLength() {
        return tslib_1.__awaiter(this, void 0, void 0, function* () {
          const buf = yield this.source.read(exports2.PADDING);
          const bb = buf && new flatbuffers_1.ByteBuffer(buf);
          const len = (bb === null || bb === void 0 ? void 0 : bb.readInt32(0)) || 0;
          return { done: len === 0, value: len };
        });
      }
      readMetadata(metadataLength) {
        return tslib_1.__awaiter(this, void 0, void 0, function* () {
          const buf = yield this.source.read(metadataLength);
          if (!buf) {
            return interfaces_js_1.ITERATOR_DONE;
          }
          if (buf.byteLength < metadataLength) {
            throw new Error(invalidMessageMetadata(metadataLength, buf.byteLength));
          }
          return { done: false, value: message_js_1.Message.decode(buf) };
        });
      }
    };
    exports2.AsyncMessageReader = AsyncMessageReader;
    var JSONMessageReader = class extends MessageReader {
      constructor(source) {
        super(new Uint8Array(0));
        this._schema = false;
        this._body = [];
        this._batchIndex = 0;
        this._dictionaryIndex = 0;
        this._json = source instanceof interfaces_js_1.ArrowJSON ? source : new interfaces_js_1.ArrowJSON(source);
      }
      next() {
        const { _json } = this;
        if (!this._schema) {
          this._schema = true;
          const message = message_js_1.Message.fromJSON(_json.schema, enum_js_1.MessageHeader.Schema);
          return { done: false, value: message };
        }
        if (this._dictionaryIndex < _json.dictionaries.length) {
          const batch = _json.dictionaries[this._dictionaryIndex++];
          this._body = batch["data"]["columns"];
          const message = message_js_1.Message.fromJSON(batch, enum_js_1.MessageHeader.DictionaryBatch);
          return { done: false, value: message };
        }
        if (this._batchIndex < _json.batches.length) {
          const batch = _json.batches[this._batchIndex++];
          this._body = batch["columns"];
          const message = message_js_1.Message.fromJSON(batch, enum_js_1.MessageHeader.RecordBatch);
          return { done: false, value: message };
        }
        this._body = [];
        return interfaces_js_1.ITERATOR_DONE;
      }
      readMessageBody(_bodyLength) {
        return flattenDataSources(this._body);
        function flattenDataSources(xs) {
          return (xs || []).reduce((buffers, column) => [
            ...buffers,
            ...column["VALIDITY"] && [column["VALIDITY"]] || [],
            ...column["TYPE_ID"] && [column["TYPE_ID"]] || [],
            ...column["OFFSET"] && [column["OFFSET"]] || [],
            ...column["DATA"] && [column["DATA"]] || [],
            ...flattenDataSources(column["children"])
          ], []);
        }
      }
      readMessage(type) {
        let r;
        if ((r = this.next()).done) {
          return null;
        }
        if (type != null && r.value.headerType !== type) {
          throw new Error(invalidMessageType(type));
        }
        return r.value;
      }
      readSchema() {
        const type = enum_js_1.MessageHeader.Schema;
        const message = this.readMessage(type);
        const schema = message === null || message === void 0 ? void 0 : message.header();
        if (!message || !schema) {
          throw new Error(nullMessage(type));
        }
        return schema;
      }
    };
    exports2.JSONMessageReader = JSONMessageReader;
    exports2.PADDING = 4;
    exports2.MAGIC_STR = "ARROW1";
    exports2.MAGIC = new Uint8Array(exports2.MAGIC_STR.length);
    for (let i = 0; i < exports2.MAGIC_STR.length; i += 1) {
      exports2.MAGIC[i] = exports2.MAGIC_STR.codePointAt(i);
    }
    function checkForMagicArrowString(buffer, index = 0) {
      for (let i = -1, n = exports2.MAGIC.length; ++i < n; ) {
        if (exports2.MAGIC[i] !== buffer[index + i]) {
          return false;
        }
      }
      return true;
    }
    exports2.checkForMagicArrowString = checkForMagicArrowString;
    exports2.magicLength = exports2.MAGIC.length;
    exports2.magicAndPadding = exports2.magicLength + exports2.PADDING;
    exports2.magicX2AndPadding = exports2.magicLength * 2 + exports2.PADDING;
  }
});

// node_modules/apache-arrow/ipc/reader.js
var require_reader = __commonJS({
  "node_modules/apache-arrow/ipc/reader.js"(exports2) {
    "use strict";
    Object.defineProperty(exports2, "__esModule", { value: true });
    exports2.AsyncRecordBatchFileReader = exports2.RecordBatchFileReader = exports2.AsyncRecordBatchStreamReader = exports2.RecordBatchStreamReader = exports2.RecordBatchReader = void 0;
    var tslib_1 = (init_tslib_es6(), __toCommonJS(tslib_es6_exports));
    var data_js_1 = require_data();
    var vector_js_1 = require_vector2();
    var type_js_1 = require_type2();
    var enum_js_1 = require_enum();
    var file_js_1 = require_file();
    var adapters_js_1 = require_adapters();
    var stream_js_1 = require_stream();
    var file_js_2 = require_file2();
    var vectorloader_js_1 = require_vectorloader();
    var recordbatch_js_1 = require_recordbatch2();
    var interfaces_js_1 = require_interfaces();
    var message_js_1 = require_message3();
    var compat_js_1 = require_compat();
    var RecordBatchReader = class _RecordBatchReader extends interfaces_js_1.ReadableInterop {
      constructor(impl) {
        super();
        this._impl = impl;
      }
      get closed() {
        return this._impl.closed;
      }
      get schema() {
        return this._impl.schema;
      }
      get autoDestroy() {
        return this._impl.autoDestroy;
      }
      get dictionaries() {
        return this._impl.dictionaries;
      }
      get numDictionaries() {
        return this._impl.numDictionaries;
      }
      get numRecordBatches() {
        return this._impl.numRecordBatches;
      }
      get footer() {
        return this._impl.isFile() ? this._impl.footer : null;
      }
      isSync() {
        return this._impl.isSync();
      }
      isAsync() {
        return this._impl.isAsync();
      }
      isFile() {
        return this._impl.isFile();
      }
      isStream() {
        return this._impl.isStream();
      }
      next() {
        return this._impl.next();
      }
      throw(value) {
        return this._impl.throw(value);
      }
      return(value) {
        return this._impl.return(value);
      }
      cancel() {
        return this._impl.cancel();
      }
      reset(schema) {
        this._impl.reset(schema);
        this._DOMStream = void 0;
        this._nodeStream = void 0;
        return this;
      }
      open(options) {
        const opening = this._impl.open(options);
        return (0, compat_js_1.isPromise)(opening) ? opening.then(() => this) : this;
      }
      readRecordBatch(index) {
        return this._impl.isFile() ? this._impl.readRecordBatch(index) : null;
      }
      [Symbol.iterator]() {
        return this._impl[Symbol.iterator]();
      }
      [Symbol.asyncIterator]() {
        return this._impl[Symbol.asyncIterator]();
      }
      toDOMStream() {
        return adapters_js_1.default.toDOMStream(this.isSync() ? { [Symbol.iterator]: () => this } : { [Symbol.asyncIterator]: () => this });
      }
      toNodeStream() {
        return adapters_js_1.default.toNodeStream(this.isSync() ? { [Symbol.iterator]: () => this } : { [Symbol.asyncIterator]: () => this }, { objectMode: true });
      }
      /** @nocollapse */
      // @ts-ignore
      static throughNode(options) {
        throw new Error(`"throughNode" not available in this environment`);
      }
      /** @nocollapse */
      static throughDOM(writableStrategy, readableStrategy) {
        throw new Error(`"throughDOM" not available in this environment`);
      }
      /** @nocollapse */
      static from(source) {
        if (source instanceof _RecordBatchReader) {
          return source;
        } else if ((0, compat_js_1.isArrowJSON)(source)) {
          return fromArrowJSON(source);
        } else if ((0, compat_js_1.isFileHandle)(source)) {
          return fromFileHandle(source);
        } else if ((0, compat_js_1.isPromise)(source)) {
          return (() => tslib_1.__awaiter(this, void 0, void 0, function* () {
            return yield _RecordBatchReader.from(yield source);
          }))();
        } else if ((0, compat_js_1.isFetchResponse)(source) || (0, compat_js_1.isReadableDOMStream)(source) || (0, compat_js_1.isReadableNodeStream)(source) || (0, compat_js_1.isAsyncIterable)(source)) {
          return fromAsyncByteStream(new stream_js_1.AsyncByteStream(source));
        }
        return fromByteStream(new stream_js_1.ByteStream(source));
      }
      /** @nocollapse */
      static readAll(source) {
        if (source instanceof _RecordBatchReader) {
          return source.isSync() ? readAllSync(source) : readAllAsync(source);
        } else if ((0, compat_js_1.isArrowJSON)(source) || ArrayBuffer.isView(source) || (0, compat_js_1.isIterable)(source) || (0, compat_js_1.isIteratorResult)(source)) {
          return readAllSync(source);
        }
        return readAllAsync(source);
      }
    };
    exports2.RecordBatchReader = RecordBatchReader;
    var RecordBatchStreamReader = class extends RecordBatchReader {
      constructor(_impl) {
        super(_impl);
        this._impl = _impl;
      }
      readAll() {
        return [...this];
      }
      [Symbol.iterator]() {
        return this._impl[Symbol.iterator]();
      }
      [Symbol.asyncIterator]() {
        return tslib_1.__asyncGenerator(this, arguments, function* _a() {
          yield tslib_1.__await(yield* tslib_1.__asyncDelegator(tslib_1.__asyncValues(this[Symbol.iterator]())));
        });
      }
    };
    exports2.RecordBatchStreamReader = RecordBatchStreamReader;
    var AsyncRecordBatchStreamReader = class extends RecordBatchReader {
      constructor(_impl) {
        super(_impl);
        this._impl = _impl;
      }
      readAll() {
        return tslib_1.__awaiter(this, void 0, void 0, function* () {
          var _a, e_1, _b, _c;
          const batches = new Array();
          try {
            for (var _d = true, _e = tslib_1.__asyncValues(this), _f; _f = yield _e.next(), _a = _f.done, !_a; _d = true) {
              _c = _f.value;
              _d = false;
              const batch = _c;
              batches.push(batch);
            }
          } catch (e_1_1) {
            e_1 = { error: e_1_1 };
          } finally {
            try {
              if (!_d && !_a && (_b = _e.return)) yield _b.call(_e);
            } finally {
              if (e_1) throw e_1.error;
            }
          }
          return batches;
        });
      }
      [Symbol.iterator]() {
        throw new Error(`AsyncRecordBatchStreamReader is not Iterable`);
      }
      [Symbol.asyncIterator]() {
        return this._impl[Symbol.asyncIterator]();
      }
    };
    exports2.AsyncRecordBatchStreamReader = AsyncRecordBatchStreamReader;
    var RecordBatchFileReader = class extends RecordBatchStreamReader {
      constructor(_impl) {
        super(_impl);
        this._impl = _impl;
      }
    };
    exports2.RecordBatchFileReader = RecordBatchFileReader;
    var AsyncRecordBatchFileReader = class extends AsyncRecordBatchStreamReader {
      constructor(_impl) {
        super(_impl);
        this._impl = _impl;
      }
    };
    exports2.AsyncRecordBatchFileReader = AsyncRecordBatchFileReader;
    var RecordBatchReaderImpl = class {
      get numDictionaries() {
        return this._dictionaryIndex;
      }
      get numRecordBatches() {
        return this._recordBatchIndex;
      }
      constructor(dictionaries = /* @__PURE__ */ new Map()) {
        this.closed = false;
        this.autoDestroy = true;
        this._dictionaryIndex = 0;
        this._recordBatchIndex = 0;
        this.dictionaries = dictionaries;
      }
      isSync() {
        return false;
      }
      isAsync() {
        return false;
      }
      isFile() {
        return false;
      }
      isStream() {
        return false;
      }
      reset(schema) {
        this._dictionaryIndex = 0;
        this._recordBatchIndex = 0;
        this.schema = schema;
        this.dictionaries = /* @__PURE__ */ new Map();
        return this;
      }
      _loadRecordBatch(header, body) {
        const children = this._loadVectors(header, body, this.schema.fields);
        const data = (0, data_js_1.makeData)({ type: new type_js_1.Struct(this.schema.fields), length: header.length, children });
        return new recordbatch_js_1.RecordBatch(this.schema, data);
      }
      _loadDictionaryBatch(header, body) {
        const { id, isDelta } = header;
        const { dictionaries, schema } = this;
        const dictionary = dictionaries.get(id);
        const type = schema.dictionaries.get(id);
        const data = this._loadVectors(header.data, body, [type]);
        return (dictionary && isDelta ? dictionary.concat(new vector_js_1.Vector(data)) : new vector_js_1.Vector(data)).memoize();
      }
      _loadVectors(header, body, types) {
        return new vectorloader_js_1.VectorLoader(body, header.nodes, header.buffers, this.dictionaries, this.schema.metadataVersion).visitMany(types);
      }
    };
    var RecordBatchStreamReaderImpl = class extends RecordBatchReaderImpl {
      constructor(source, dictionaries) {
        super(dictionaries);
        this._reader = !(0, compat_js_1.isArrowJSON)(source) ? new message_js_1.MessageReader(this._handle = source) : new message_js_1.JSONMessageReader(this._handle = source);
      }
      isSync() {
        return true;
      }
      isStream() {
        return true;
      }
      [Symbol.iterator]() {
        return this;
      }
      cancel() {
        if (!this.closed && (this.closed = true)) {
          this.reset()._reader.return();
          this._reader = null;
          this.dictionaries = null;
        }
      }
      open(options) {
        if (!this.closed) {
          this.autoDestroy = shouldAutoDestroy(this, options);
          if (!(this.schema || (this.schema = this._reader.readSchema()))) {
            this.cancel();
          }
        }
        return this;
      }
      throw(value) {
        if (!this.closed && this.autoDestroy && (this.closed = true)) {
          return this.reset()._reader.throw(value);
        }
        return interfaces_js_1.ITERATOR_DONE;
      }
      return(value) {
        if (!this.closed && this.autoDestroy && (this.closed = true)) {
          return this.reset()._reader.return(value);
        }
        return interfaces_js_1.ITERATOR_DONE;
      }
      next() {
        if (this.closed) {
          return interfaces_js_1.ITERATOR_DONE;
        }
        let message;
        const { _reader: reader } = this;
        while (message = this._readNextMessageAndValidate()) {
          if (message.isSchema()) {
            this.reset(message.header());
          } else if (message.isRecordBatch()) {
            this._recordBatchIndex++;
            const header = message.header();
            const buffer = reader.readMessageBody(message.bodyLength);
            const recordBatch = this._loadRecordBatch(header, buffer);
            return { done: false, value: recordBatch };
          } else if (message.isDictionaryBatch()) {
            this._dictionaryIndex++;
            const header = message.header();
            const buffer = reader.readMessageBody(message.bodyLength);
            const vector = this._loadDictionaryBatch(header, buffer);
            this.dictionaries.set(header.id, vector);
          }
        }
        if (this.schema && this._recordBatchIndex === 0) {
          this._recordBatchIndex++;
          return { done: false, value: new recordbatch_js_1._InternalEmptyPlaceholderRecordBatch(this.schema) };
        }
        return this.return();
      }
      _readNextMessageAndValidate(type) {
        return this._reader.readMessage(type);
      }
    };
    var AsyncRecordBatchStreamReaderImpl = class extends RecordBatchReaderImpl {
      constructor(source, dictionaries) {
        super(dictionaries);
        this._reader = new message_js_1.AsyncMessageReader(this._handle = source);
      }
      isAsync() {
        return true;
      }
      isStream() {
        return true;
      }
      [Symbol.asyncIterator]() {
        return this;
      }
      cancel() {
        return tslib_1.__awaiter(this, void 0, void 0, function* () {
          if (!this.closed && (this.closed = true)) {
            yield this.reset()._reader.return();
            this._reader = null;
            this.dictionaries = null;
          }
        });
      }
      open(options) {
        return tslib_1.__awaiter(this, void 0, void 0, function* () {
          if (!this.closed) {
            this.autoDestroy = shouldAutoDestroy(this, options);
            if (!(this.schema || (this.schema = yield this._reader.readSchema()))) {
              yield this.cancel();
            }
          }
          return this;
        });
      }
      throw(value) {
        return tslib_1.__awaiter(this, void 0, void 0, function* () {
          if (!this.closed && this.autoDestroy && (this.closed = true)) {
            return yield this.reset()._reader.throw(value);
          }
          return interfaces_js_1.ITERATOR_DONE;
        });
      }
      return(value) {
        return tslib_1.__awaiter(this, void 0, void 0, function* () {
          if (!this.closed && this.autoDestroy && (this.closed = true)) {
            return yield this.reset()._reader.return(value);
          }
          return interfaces_js_1.ITERATOR_DONE;
        });
      }
      next() {
        return tslib_1.__awaiter(this, void 0, void 0, function* () {
          if (this.closed) {
            return interfaces_js_1.ITERATOR_DONE;
          }
          let message;
          const { _reader: reader } = this;
          while (message = yield this._readNextMessageAndValidate()) {
            if (message.isSchema()) {
              yield this.reset(message.header());
            } else if (message.isRecordBatch()) {
              this._recordBatchIndex++;
              const header = message.header();
              const buffer = yield reader.readMessageBody(message.bodyLength);
              const recordBatch = this._loadRecordBatch(header, buffer);
              return { done: false, value: recordBatch };
            } else if (message.isDictionaryBatch()) {
              this._dictionaryIndex++;
              const header = message.header();
              const buffer = yield reader.readMessageBody(message.bodyLength);
              const vector = this._loadDictionaryBatch(header, buffer);
              this.dictionaries.set(header.id, vector);
            }
          }
          if (this.schema && this._recordBatchIndex === 0) {
            this._recordBatchIndex++;
            return { done: false, value: new recordbatch_js_1._InternalEmptyPlaceholderRecordBatch(this.schema) };
          }
          return yield this.return();
        });
      }
      _readNextMessageAndValidate(type) {
        return tslib_1.__awaiter(this, void 0, void 0, function* () {
          return yield this._reader.readMessage(type);
        });
      }
    };
    var RecordBatchFileReaderImpl = class extends RecordBatchStreamReaderImpl {
      get footer() {
        return this._footer;
      }
      get numDictionaries() {
        return this._footer ? this._footer.numDictionaries : 0;
      }
      get numRecordBatches() {
        return this._footer ? this._footer.numRecordBatches : 0;
      }
      constructor(source, dictionaries) {
        super(source instanceof file_js_2.RandomAccessFile ? source : new file_js_2.RandomAccessFile(source), dictionaries);
      }
      isSync() {
        return true;
      }
      isFile() {
        return true;
      }
      open(options) {
        if (!this.closed && !this._footer) {
          this.schema = (this._footer = this._readFooter()).schema;
          for (const block of this._footer.dictionaryBatches()) {
            block && this._readDictionaryBatch(this._dictionaryIndex++);
          }
        }
        return super.open(options);
      }
      readRecordBatch(index) {
        var _a;
        if (this.closed) {
          return null;
        }
        if (!this._footer) {
          this.open();
        }
        const block = (_a = this._footer) === null || _a === void 0 ? void 0 : _a.getRecordBatch(index);
        if (block && this._handle.seek(block.offset)) {
          const message = this._reader.readMessage(enum_js_1.MessageHeader.RecordBatch);
          if (message === null || message === void 0 ? void 0 : message.isRecordBatch()) {
            const header = message.header();
            const buffer = this._reader.readMessageBody(message.bodyLength);
            const recordBatch = this._loadRecordBatch(header, buffer);
            return recordBatch;
          }
        }
        return null;
      }
      _readDictionaryBatch(index) {
        var _a;
        const block = (_a = this._footer) === null || _a === void 0 ? void 0 : _a.getDictionaryBatch(index);
        if (block && this._handle.seek(block.offset)) {
          const message = this._reader.readMessage(enum_js_1.MessageHeader.DictionaryBatch);
          if (message === null || message === void 0 ? void 0 : message.isDictionaryBatch()) {
            const header = message.header();
            const buffer = this._reader.readMessageBody(message.bodyLength);
            const vector = this._loadDictionaryBatch(header, buffer);
            this.dictionaries.set(header.id, vector);
          }
        }
      }
      _readFooter() {
        const { _handle } = this;
        const offset = _handle.size - message_js_1.magicAndPadding;
        const length = _handle.readInt32(offset);
        const buffer = _handle.readAt(offset - length, length);
        return file_js_1.Footer.decode(buffer);
      }
      _readNextMessageAndValidate(type) {
        var _a;
        if (!this._footer) {
          this.open();
        }
        if (this._footer && this._recordBatchIndex < this.numRecordBatches) {
          const block = (_a = this._footer) === null || _a === void 0 ? void 0 : _a.getRecordBatch(this._recordBatchIndex);
          if (block && this._handle.seek(block.offset)) {
            return this._reader.readMessage(type);
          }
        }
        return null;
      }
    };
    var AsyncRecordBatchFileReaderImpl = class extends AsyncRecordBatchStreamReaderImpl {
      get footer() {
        return this._footer;
      }
      get numDictionaries() {
        return this._footer ? this._footer.numDictionaries : 0;
      }
      get numRecordBatches() {
        return this._footer ? this._footer.numRecordBatches : 0;
      }
      constructor(source, ...rest) {
        const byteLength = typeof rest[0] !== "number" ? rest.shift() : void 0;
        const dictionaries = rest[0] instanceof Map ? rest.shift() : void 0;
        super(source instanceof file_js_2.AsyncRandomAccessFile ? source : new file_js_2.AsyncRandomAccessFile(source, byteLength), dictionaries);
      }
      isFile() {
        return true;
      }
      isAsync() {
        return true;
      }
      open(options) {
        const _super = Object.create(null, {
          open: { get: () => super.open }
        });
        return tslib_1.__awaiter(this, void 0, void 0, function* () {
          if (!this.closed && !this._footer) {
            this.schema = (this._footer = yield this._readFooter()).schema;
            for (const block of this._footer.dictionaryBatches()) {
              block && (yield this._readDictionaryBatch(this._dictionaryIndex++));
            }
          }
          return yield _super.open.call(this, options);
        });
      }
      readRecordBatch(index) {
        return tslib_1.__awaiter(this, void 0, void 0, function* () {
          var _a;
          if (this.closed) {
            return null;
          }
          if (!this._footer) {
            yield this.open();
          }
          const block = (_a = this._footer) === null || _a === void 0 ? void 0 : _a.getRecordBatch(index);
          if (block && (yield this._handle.seek(block.offset))) {
            const message = yield this._reader.readMessage(enum_js_1.MessageHeader.RecordBatch);
            if (message === null || message === void 0 ? void 0 : message.isRecordBatch()) {
              const header = message.header();
              const buffer = yield this._reader.readMessageBody(message.bodyLength);
              const recordBatch = this._loadRecordBatch(header, buffer);
              return recordBatch;
            }
          }
          return null;
        });
      }
      _readDictionaryBatch(index) {
        return tslib_1.__awaiter(this, void 0, void 0, function* () {
          var _a;
          const block = (_a = this._footer) === null || _a === void 0 ? void 0 : _a.getDictionaryBatch(index);
          if (block && (yield this._handle.seek(block.offset))) {
            const message = yield this._reader.readMessage(enum_js_1.MessageHeader.DictionaryBatch);
            if (message === null || message === void 0 ? void 0 : message.isDictionaryBatch()) {
              const header = message.header();
              const buffer = yield this._reader.readMessageBody(message.bodyLength);
              const vector = this._loadDictionaryBatch(header, buffer);
              this.dictionaries.set(header.id, vector);
            }
          }
        });
      }
      _readFooter() {
        return tslib_1.__awaiter(this, void 0, void 0, function* () {
          const { _handle } = this;
          _handle._pending && (yield _handle._pending);
          const offset = _handle.size - message_js_1.magicAndPadding;
          const length = yield _handle.readInt32(offset);
          const buffer = yield _handle.readAt(offset - length, length);
          return file_js_1.Footer.decode(buffer);
        });
      }
      _readNextMessageAndValidate(type) {
        return tslib_1.__awaiter(this, void 0, void 0, function* () {
          if (!this._footer) {
            yield this.open();
          }
          if (this._footer && this._recordBatchIndex < this.numRecordBatches) {
            const block = this._footer.getRecordBatch(this._recordBatchIndex);
            if (block && (yield this._handle.seek(block.offset))) {
              return yield this._reader.readMessage(type);
            }
          }
          return null;
        });
      }
    };
    var RecordBatchJSONReaderImpl = class extends RecordBatchStreamReaderImpl {
      constructor(source, dictionaries) {
        super(source, dictionaries);
      }
      _loadVectors(header, body, types) {
        return new vectorloader_js_1.JSONVectorLoader(body, header.nodes, header.buffers, this.dictionaries, this.schema.metadataVersion).visitMany(types);
      }
    };
    function shouldAutoDestroy(self, options) {
      return options && typeof options["autoDestroy"] === "boolean" ? options["autoDestroy"] : self["autoDestroy"];
    }
    function* readAllSync(source) {
      const reader = RecordBatchReader.from(source);
      try {
        if (!reader.open({ autoDestroy: false }).closed) {
          do {
            yield reader;
          } while (!reader.reset().open().closed);
        }
      } finally {
        reader.cancel();
      }
    }
    function readAllAsync(source) {
      return tslib_1.__asyncGenerator(this, arguments, function* readAllAsync_1() {
        const reader = yield tslib_1.__await(RecordBatchReader.from(source));
        try {
          if (!(yield tslib_1.__await(reader.open({ autoDestroy: false }))).closed) {
            do {
              yield yield tslib_1.__await(reader);
            } while (!(yield tslib_1.__await(reader.reset().open())).closed);
          }
        } finally {
          yield tslib_1.__await(reader.cancel());
        }
      });
    }
    function fromArrowJSON(source) {
      return new RecordBatchStreamReader(new RecordBatchJSONReaderImpl(source));
    }
    function fromByteStream(source) {
      const bytes = source.peek(message_js_1.magicLength + 7 & ~7);
      return bytes && bytes.byteLength >= 4 ? !(0, message_js_1.checkForMagicArrowString)(bytes) ? new RecordBatchStreamReader(new RecordBatchStreamReaderImpl(source)) : new RecordBatchFileReader(new RecordBatchFileReaderImpl(source.read())) : new RecordBatchStreamReader(new RecordBatchStreamReaderImpl((function* () {
      })()));
    }
    function fromAsyncByteStream(source) {
      return tslib_1.__awaiter(this, void 0, void 0, function* () {
        const bytes = yield source.peek(message_js_1.magicLength + 7 & ~7);
        return bytes && bytes.byteLength >= 4 ? !(0, message_js_1.checkForMagicArrowString)(bytes) ? new AsyncRecordBatchStreamReader(new AsyncRecordBatchStreamReaderImpl(source)) : new RecordBatchFileReader(new RecordBatchFileReaderImpl(yield source.read())) : new AsyncRecordBatchStreamReader(new AsyncRecordBatchStreamReaderImpl((function() {
          return tslib_1.__asyncGenerator(this, arguments, function* () {
          });
        })()));
      });
    }
    function fromFileHandle(source) {
      return tslib_1.__awaiter(this, void 0, void 0, function* () {
        const { size } = yield source.stat();
        const file = new file_js_2.AsyncRandomAccessFile(source, size);
        if (size >= message_js_1.magicX2AndPadding && (0, message_js_1.checkForMagicArrowString)(yield file.readAt(0, message_js_1.magicLength + 7 & ~7))) {
          return new AsyncRecordBatchFileReader(new AsyncRecordBatchFileReaderImpl(file));
        }
        return new AsyncRecordBatchStreamReader(new AsyncRecordBatchStreamReaderImpl(file));
      });
    }
  }
});

// node_modules/apache-arrow/visitor/vectorassembler.js
var require_vectorassembler = __commonJS({
  "node_modules/apache-arrow/visitor/vectorassembler.js"(exports2) {
    "use strict";
    Object.defineProperty(exports2, "__esModule", { value: true });
    exports2.VectorAssembler = void 0;
    var vector_js_1 = require_vector2();
    var visitor_js_1 = require_visitor();
    var enum_js_1 = require_enum();
    var recordbatch_js_1 = require_recordbatch2();
    var buffer_js_1 = require_buffer();
    var bit_js_1 = require_bit();
    var message_js_1 = require_message2();
    var type_js_1 = require_type2();
    var bigint_js_1 = require_bigint();
    var VectorAssembler = class _VectorAssembler extends visitor_js_1.Visitor {
      /** @nocollapse */
      static assemble(...args) {
        const unwrap = (nodes) => nodes.flatMap((node) => Array.isArray(node) ? unwrap(node) : node instanceof recordbatch_js_1.RecordBatch ? node.data.children : node.data);
        const assembler = new _VectorAssembler();
        assembler.visitMany(unwrap(args));
        return assembler;
      }
      constructor() {
        super();
        this._byteLength = 0;
        this._nodes = [];
        this._buffers = [];
        this._bufferRegions = [];
      }
      visit(data) {
        if (data instanceof vector_js_1.Vector) {
          this.visitMany(data.data);
          return this;
        }
        const { type } = data;
        if (!type_js_1.DataType.isDictionary(type)) {
          const { length } = data;
          if (length > 2147483647) {
            throw new RangeError("Cannot write arrays larger than 2^31 - 1 in length");
          }
          if (type_js_1.DataType.isUnion(type)) {
            this.nodes.push(new message_js_1.FieldNode(length, 0));
          } else {
            const { nullCount } = data;
            if (!type_js_1.DataType.isNull(type)) {
              addBuffer.call(this, nullCount <= 0 ? new Uint8Array(0) : (0, bit_js_1.truncateBitmap)(data.offset, length, data.nullBitmap));
            }
            this.nodes.push(new message_js_1.FieldNode(length, nullCount));
          }
        }
        return super.visit(data);
      }
      visitNull(_null) {
        return this;
      }
      visitDictionary(data) {
        return this.visit(data.clone(data.type.indices));
      }
      get nodes() {
        return this._nodes;
      }
      get buffers() {
        return this._buffers;
      }
      get byteLength() {
        return this._byteLength;
      }
      get bufferRegions() {
        return this._bufferRegions;
      }
    };
    exports2.VectorAssembler = VectorAssembler;
    function addBuffer(values) {
      const byteLength = values.byteLength + 7 & ~7;
      this.buffers.push(values);
      this.bufferRegions.push(new message_js_1.BufferRegion(this._byteLength, byteLength));
      this._byteLength += byteLength;
      return this;
    }
    function assembleUnion(data) {
      var _a;
      const { type, length, typeIds, valueOffsets } = data;
      addBuffer.call(this, typeIds);
      if (type.mode === enum_js_1.UnionMode.Sparse) {
        return assembleNestedVector.call(this, data);
      } else if (type.mode === enum_js_1.UnionMode.Dense) {
        if (data.offset <= 0) {
          addBuffer.call(this, valueOffsets);
          return assembleNestedVector.call(this, data);
        } else {
          const shiftedOffsets = new Int32Array(length);
          const childOffsets = /* @__PURE__ */ Object.create(null);
          const childLengths = /* @__PURE__ */ Object.create(null);
          for (let typeId, shift, index = -1; ++index < length; ) {
            if ((typeId = typeIds[index]) === void 0) {
              continue;
            }
            if ((shift = childOffsets[typeId]) === void 0) {
              shift = childOffsets[typeId] = valueOffsets[index];
            }
            shiftedOffsets[index] = valueOffsets[index] - shift;
            childLengths[typeId] = ((_a = childLengths[typeId]) !== null && _a !== void 0 ? _a : 0) + 1;
          }
          addBuffer.call(this, shiftedOffsets);
          this.visitMany(data.children.map((child, childIndex) => {
            const typeId = type.typeIds[childIndex];
            const childOffset = childOffsets[typeId];
            const childLength = childLengths[typeId];
            return child.slice(childOffset, Math.min(length, childLength));
          }));
        }
      }
      return this;
    }
    function assembleBoolVector(data) {
      let values;
      if (data.nullCount >= data.length) {
        return addBuffer.call(this, new Uint8Array(0));
      } else if ((values = data.values) instanceof Uint8Array) {
        return addBuffer.call(this, (0, bit_js_1.truncateBitmap)(data.offset, data.length, values));
      }
      return addBuffer.call(this, (0, bit_js_1.packBools)(data.values));
    }
    function assembleFlatVector(data) {
      return addBuffer.call(this, data.values.subarray(0, data.length * data.stride));
    }
    function assembleFlatListVector(data) {
      const { length, values, valueOffsets } = data;
      const begin = (0, bigint_js_1.bigIntToNumber)(valueOffsets[0]);
      const end = (0, bigint_js_1.bigIntToNumber)(valueOffsets[length]);
      const byteLength = Math.min(end - begin, values.byteLength - begin);
      addBuffer.call(this, (0, buffer_js_1.rebaseValueOffsets)(-begin, length + 1, valueOffsets));
      addBuffer.call(this, values.subarray(begin, begin + byteLength));
      return this;
    }
    function assembleListVector(data) {
      const { length, valueOffsets } = data;
      if (valueOffsets) {
        const { [0]: begin, [length]: end } = valueOffsets;
        addBuffer.call(this, (0, buffer_js_1.rebaseValueOffsets)(-begin, length + 1, valueOffsets));
        return this.visit(data.children[0].slice(begin, end - begin));
      }
      return this.visit(data.children[0]);
    }
    function assembleNestedVector(data) {
      return this.visitMany(data.type.children.map((_, i) => data.children[i]).filter(Boolean))[0];
    }
    VectorAssembler.prototype.visitBool = assembleBoolVector;
    VectorAssembler.prototype.visitInt = assembleFlatVector;
    VectorAssembler.prototype.visitFloat = assembleFlatVector;
    VectorAssembler.prototype.visitUtf8 = assembleFlatListVector;
    VectorAssembler.prototype.visitLargeUtf8 = assembleFlatListVector;
    VectorAssembler.prototype.visitBinary = assembleFlatListVector;
    VectorAssembler.prototype.visitLargeBinary = assembleFlatListVector;
    VectorAssembler.prototype.visitFixedSizeBinary = assembleFlatVector;
    VectorAssembler.prototype.visitDate = assembleFlatVector;
    VectorAssembler.prototype.visitTimestamp = assembleFlatVector;
    VectorAssembler.prototype.visitTime = assembleFlatVector;
    VectorAssembler.prototype.visitDecimal = assembleFlatVector;
    VectorAssembler.prototype.visitList = assembleListVector;
    VectorAssembler.prototype.visitStruct = assembleNestedVector;
    VectorAssembler.prototype.visitUnion = assembleUnion;
    VectorAssembler.prototype.visitInterval = assembleFlatVector;
    VectorAssembler.prototype.visitDuration = assembleFlatVector;
    VectorAssembler.prototype.visitFixedSizeList = assembleListVector;
    VectorAssembler.prototype.visitMap = assembleListVector;
  }
});

// node_modules/apache-arrow/visitor/jsontypeassembler.js
var require_jsontypeassembler = __commonJS({
  "node_modules/apache-arrow/visitor/jsontypeassembler.js"(exports2) {
    "use strict";
    Object.defineProperty(exports2, "__esModule", { value: true });
    exports2.JSONTypeAssembler = void 0;
    var visitor_js_1 = require_visitor();
    var type_js_1 = require_type();
    var enum_js_1 = require_enum();
    var JSONTypeAssembler = class extends visitor_js_1.Visitor {
      visit(node) {
        return node == null ? void 0 : super.visit(node);
      }
      visitNull({ typeId }) {
        return { "name": type_js_1.Type[typeId].toLowerCase() };
      }
      visitInt({ typeId, bitWidth, isSigned }) {
        return { "name": type_js_1.Type[typeId].toLowerCase(), "bitWidth": bitWidth, "isSigned": isSigned };
      }
      visitFloat({ typeId, precision }) {
        return { "name": type_js_1.Type[typeId].toLowerCase(), "precision": enum_js_1.Precision[precision] };
      }
      visitBinary({ typeId }) {
        return { "name": type_js_1.Type[typeId].toLowerCase() };
      }
      visitLargeBinary({ typeId }) {
        return { "name": type_js_1.Type[typeId].toLowerCase() };
      }
      visitBool({ typeId }) {
        return { "name": type_js_1.Type[typeId].toLowerCase() };
      }
      visitUtf8({ typeId }) {
        return { "name": type_js_1.Type[typeId].toLowerCase() };
      }
      visitLargeUtf8({ typeId }) {
        return { "name": type_js_1.Type[typeId].toLowerCase() };
      }
      visitDecimal({ typeId, scale, precision, bitWidth }) {
        return { "name": type_js_1.Type[typeId].toLowerCase(), "scale": scale, "precision": precision, "bitWidth": bitWidth };
      }
      visitDate({ typeId, unit }) {
        return { "name": type_js_1.Type[typeId].toLowerCase(), "unit": enum_js_1.DateUnit[unit] };
      }
      visitTime({ typeId, unit, bitWidth }) {
        return { "name": type_js_1.Type[typeId].toLowerCase(), "unit": enum_js_1.TimeUnit[unit], bitWidth };
      }
      visitTimestamp({ typeId, timezone, unit }) {
        return { "name": type_js_1.Type[typeId].toLowerCase(), "unit": enum_js_1.TimeUnit[unit], timezone };
      }
      visitInterval({ typeId, unit }) {
        return { "name": type_js_1.Type[typeId].toLowerCase(), "unit": enum_js_1.IntervalUnit[unit] };
      }
      visitDuration({ typeId, unit }) {
        return { "name": type_js_1.Type[typeId].toLocaleLowerCase(), "unit": enum_js_1.TimeUnit[unit] };
      }
      visitList({ typeId }) {
        return { "name": type_js_1.Type[typeId].toLowerCase() };
      }
      visitStruct({ typeId }) {
        return { "name": type_js_1.Type[typeId].toLowerCase() };
      }
      visitUnion({ typeId, mode, typeIds }) {
        return {
          "name": type_js_1.Type[typeId].toLowerCase(),
          "mode": enum_js_1.UnionMode[mode].toUpperCase(),
          "typeIds": [...typeIds]
        };
      }
      visitDictionary(node) {
        return this.visit(node.dictionary);
      }
      visitFixedSizeBinary({ typeId, byteWidth }) {
        return { "name": type_js_1.Type[typeId].toLowerCase(), "byteWidth": byteWidth };
      }
      visitFixedSizeList({ typeId, listSize }) {
        return { "name": type_js_1.Type[typeId].toLowerCase(), "listSize": listSize };
      }
      visitMap({ typeId, keysSorted }) {
        return { "name": type_js_1.Type[typeId].toLowerCase(), "keysSorted": keysSorted };
      }
    };
    exports2.JSONTypeAssembler = JSONTypeAssembler;
  }
});

// node_modules/apache-arrow/visitor/jsonvectorassembler.js
var require_jsonvectorassembler = __commonJS({
  "node_modules/apache-arrow/visitor/jsonvectorassembler.js"(exports2) {
    "use strict";
    Object.defineProperty(exports2, "__esModule", { value: true });
    exports2.JSONVectorAssembler = void 0;
    var bn_js_1 = require_bn();
    var vector_js_1 = require_vector2();
    var visitor_js_1 = require_visitor();
    var enum_js_1 = require_enum();
    var enum_js_2 = require_enum();
    var bit_js_1 = require_bit();
    var type_js_1 = require_type2();
    var JSONVectorAssembler = class _JSONVectorAssembler extends visitor_js_1.Visitor {
      /** @nocollapse */
      static assemble(...batches) {
        const assembler = new _JSONVectorAssembler();
        return batches.map(({ schema, data }) => {
          return assembler.visitMany(schema.fields, data.children);
        });
      }
      visit({ name }, data) {
        const { length } = data;
        const { offset, nullCount, nullBitmap } = data;
        const type = type_js_1.DataType.isDictionary(data.type) ? data.type.indices : data.type;
        const buffers = Object.assign([], data.buffers, { [enum_js_1.BufferType.VALIDITY]: void 0 });
        return Object.assign({ "name": name, "count": length, "VALIDITY": type_js_1.DataType.isNull(type) || type_js_1.DataType.isUnion(type) ? void 0 : nullCount <= 0 ? Array.from({ length }, () => 1) : [...new bit_js_1.BitIterator(nullBitmap, offset, length, null, bit_js_1.getBit)] }, super.visit(data.clone(type, offset, length, 0, buffers)));
      }
      visitNull() {
        return {};
      }
      visitBool({ values, offset, length }) {
        return { "DATA": [...new bit_js_1.BitIterator(values, offset, length, null, bit_js_1.getBool)] };
      }
      visitInt(data) {
        return {
          "DATA": data.type.bitWidth < 64 ? [...data.values] : [...bigNumsToStrings(data.values, 2)]
        };
      }
      visitFloat(data) {
        return { "DATA": [...data.values] };
      }
      visitUtf8(data) {
        return { "DATA": [...new vector_js_1.Vector([data])], "OFFSET": [...data.valueOffsets] };
      }
      visitLargeUtf8(data) {
        return { "DATA": [...new vector_js_1.Vector([data])], "OFFSET": [...bigNumsToStrings(data.valueOffsets, 2)] };
      }
      visitBinary(data) {
        return { "DATA": [...binaryToString(new vector_js_1.Vector([data]))], "OFFSET": [...data.valueOffsets] };
      }
      visitLargeBinary(data) {
        return { "DATA": [...binaryToString(new vector_js_1.Vector([data]))], "OFFSET": [...bigNumsToStrings(data.valueOffsets, 2)] };
      }
      visitFixedSizeBinary(data) {
        return { "DATA": [...binaryToString(new vector_js_1.Vector([data]))] };
      }
      visitDate(data) {
        return {
          "DATA": data.type.unit === enum_js_2.DateUnit.DAY ? [...data.values] : [...bigNumsToStrings(data.values, 2)]
        };
      }
      visitTimestamp(data) {
        return { "DATA": [...bigNumsToStrings(data.values, 2)] };
      }
      visitTime(data) {
        return {
          "DATA": data.type.unit < enum_js_2.TimeUnit.MICROSECOND ? [...data.values] : [...bigNumsToStrings(data.values, 2)]
        };
      }
      visitDecimal(data) {
        return { "DATA": [...bigNumsToStrings(data.values, 4)] };
      }
      visitList(data) {
        return {
          "OFFSET": [...data.valueOffsets],
          "children": this.visitMany(data.type.children, data.children)
        };
      }
      visitStruct(data) {
        return {
          "children": this.visitMany(data.type.children, data.children)
        };
      }
      visitUnion(data) {
        return {
          "TYPE_ID": [...data.typeIds],
          "OFFSET": data.type.mode === enum_js_2.UnionMode.Dense ? [...data.valueOffsets] : void 0,
          "children": this.visitMany(data.type.children, data.children)
        };
      }
      visitInterval(data) {
        return { "DATA": [...data.values] };
      }
      visitDuration(data) {
        return { "DATA": [...bigNumsToStrings(data.values, 2)] };
      }
      visitFixedSizeList(data) {
        return {
          "children": this.visitMany(data.type.children, data.children)
        };
      }
      visitMap(data) {
        return {
          "OFFSET": [...data.valueOffsets],
          "children": this.visitMany(data.type.children, data.children)
        };
      }
    };
    exports2.JSONVectorAssembler = JSONVectorAssembler;
    function* binaryToString(vector) {
      for (const octets of vector) {
        yield octets.reduce((str, byte) => {
          return `${str}${("0" + (byte & 255).toString(16)).slice(-2)}`;
        }, "").toUpperCase();
      }
    }
    function* bigNumsToStrings(values, stride) {
      const u32s = new Uint32Array(values.buffer);
      for (let i = -1, n = u32s.length / stride; ++i < n; ) {
        yield `${bn_js_1.BN.new(u32s.subarray((i + 0) * stride, (i + 1) * stride), false)}`;
      }
    }
  }
});

// node_modules/apache-arrow/ipc/writer.js
var require_writer = __commonJS({
  "node_modules/apache-arrow/ipc/writer.js"(exports2) {
    "use strict";
    Object.defineProperty(exports2, "__esModule", { value: true });
    exports2.RecordBatchJSONWriter = exports2.RecordBatchFileWriter = exports2.RecordBatchStreamWriter = exports2.RecordBatchWriter = void 0;
    var tslib_1 = (init_tslib_es6(), __toCommonJS(tslib_es6_exports));
    var table_js_1 = require_table();
    var message_js_1 = require_message3();
    var vector_js_1 = require_vector2();
    var type_js_1 = require_type2();
    var message_js_2 = require_message2();
    var metadata = require_message2();
    var file_js_1 = require_file();
    var enum_js_1 = require_enum();
    var typecomparator_js_1 = require_typecomparator();
    var stream_js_1 = require_stream();
    var vectorassembler_js_1 = require_vectorassembler();
    var jsontypeassembler_js_1 = require_jsontypeassembler();
    var jsonvectorassembler_js_1 = require_jsonvectorassembler();
    var buffer_js_1 = require_buffer();
    var recordbatch_js_1 = require_recordbatch2();
    var interfaces_js_1 = require_interfaces();
    var compat_js_1 = require_compat();
    var RecordBatchWriter = class extends interfaces_js_1.ReadableInterop {
      /** @nocollapse */
      // @ts-ignore
      static throughNode(options) {
        throw new Error(`"throughNode" not available in this environment`);
      }
      /** @nocollapse */
      static throughDOM(writableStrategy, readableStrategy) {
        throw new Error(`"throughDOM" not available in this environment`);
      }
      constructor(options) {
        super();
        this._position = 0;
        this._started = false;
        this._sink = new stream_js_1.AsyncByteQueue();
        this._schema = null;
        this._dictionaryBlocks = [];
        this._recordBatchBlocks = [];
        this._seenDictionaries = /* @__PURE__ */ new Map();
        this._dictionaryDeltaOffsets = /* @__PURE__ */ new Map();
        (0, compat_js_1.isObject)(options) || (options = { autoDestroy: true, writeLegacyIpcFormat: false });
        this._autoDestroy = typeof options.autoDestroy === "boolean" ? options.autoDestroy : true;
        this._writeLegacyIpcFormat = typeof options.writeLegacyIpcFormat === "boolean" ? options.writeLegacyIpcFormat : false;
      }
      toString(sync = false) {
        return this._sink.toString(sync);
      }
      toUint8Array(sync = false) {
        return this._sink.toUint8Array(sync);
      }
      writeAll(input) {
        if ((0, compat_js_1.isPromise)(input)) {
          return input.then((x) => this.writeAll(x));
        } else if ((0, compat_js_1.isAsyncIterable)(input)) {
          return writeAllAsync(this, input);
        }
        return writeAll(this, input);
      }
      get closed() {
        return this._sink.closed;
      }
      [Symbol.asyncIterator]() {
        return this._sink[Symbol.asyncIterator]();
      }
      toDOMStream(options) {
        return this._sink.toDOMStream(options);
      }
      toNodeStream(options) {
        return this._sink.toNodeStream(options);
      }
      close() {
        return this.reset()._sink.close();
      }
      abort(reason) {
        return this.reset()._sink.abort(reason);
      }
      finish() {
        this._autoDestroy ? this.close() : this.reset(this._sink, this._schema);
        return this;
      }
      reset(sink = this._sink, schema = null) {
        if (sink === this._sink || sink instanceof stream_js_1.AsyncByteQueue) {
          this._sink = sink;
        } else {
          this._sink = new stream_js_1.AsyncByteQueue();
          if (sink && (0, compat_js_1.isWritableDOMStream)(sink)) {
            this.toDOMStream({ type: "bytes" }).pipeTo(sink);
          } else if (sink && (0, compat_js_1.isWritableNodeStream)(sink)) {
            this.toNodeStream({ objectMode: false }).pipe(sink);
          }
        }
        if (this._started && this._schema) {
          this._writeFooter(this._schema);
        }
        this._started = false;
        this._dictionaryBlocks = [];
        this._recordBatchBlocks = [];
        this._seenDictionaries = /* @__PURE__ */ new Map();
        this._dictionaryDeltaOffsets = /* @__PURE__ */ new Map();
        if (!schema || !(0, typecomparator_js_1.compareSchemas)(schema, this._schema)) {
          if (schema == null) {
            this._position = 0;
            this._schema = null;
          } else {
            this._started = true;
            this._schema = schema;
            this._writeSchema(schema);
          }
        }
        return this;
      }
      write(payload) {
        let schema = null;
        if (!this._sink) {
          throw new Error(`RecordBatchWriter is closed`);
        } else if (payload == null) {
          return this.finish() && void 0;
        } else if (payload instanceof table_js_1.Table && !(schema = payload.schema)) {
          return this.finish() && void 0;
        } else if (payload instanceof recordbatch_js_1.RecordBatch && !(schema = payload.schema)) {
          return this.finish() && void 0;
        }
        if (schema && !(0, typecomparator_js_1.compareSchemas)(schema, this._schema)) {
          if (this._started && this._autoDestroy) {
            return this.close();
          }
          this.reset(this._sink, schema);
        }
        if (payload instanceof recordbatch_js_1.RecordBatch) {
          if (!(payload instanceof recordbatch_js_1._InternalEmptyPlaceholderRecordBatch)) {
            this._writeRecordBatch(payload);
          }
        } else if (payload instanceof table_js_1.Table) {
          this.writeAll(payload.batches);
        } else if ((0, compat_js_1.isIterable)(payload)) {
          this.writeAll(payload);
        }
      }
      _writeMessage(message, alignment = 8) {
        const a = alignment - 1;
        const buffer = message_js_2.Message.encode(message);
        const flatbufferSize = buffer.byteLength;
        const prefixSize = !this._writeLegacyIpcFormat ? 8 : 4;
        const alignedSize = flatbufferSize + prefixSize + a & ~a;
        const nPaddingBytes = alignedSize - flatbufferSize - prefixSize;
        if (message.headerType === enum_js_1.MessageHeader.RecordBatch) {
          this._recordBatchBlocks.push(new file_js_1.FileBlock(alignedSize, message.bodyLength, this._position));
        } else if (message.headerType === enum_js_1.MessageHeader.DictionaryBatch) {
          this._dictionaryBlocks.push(new file_js_1.FileBlock(alignedSize, message.bodyLength, this._position));
        }
        if (!this._writeLegacyIpcFormat) {
          this._write(Int32Array.of(-1));
        }
        this._write(Int32Array.of(alignedSize - prefixSize));
        if (flatbufferSize > 0) {
          this._write(buffer);
        }
        return this._writePadding(nPaddingBytes);
      }
      _write(chunk) {
        if (this._started) {
          const buffer = (0, buffer_js_1.toUint8Array)(chunk);
          if (buffer && buffer.byteLength > 0) {
            this._sink.write(buffer);
            this._position += buffer.byteLength;
          }
        }
        return this;
      }
      _writeSchema(schema) {
        return this._writeMessage(message_js_2.Message.from(schema));
      }
      // @ts-ignore
      _writeFooter(schema) {
        return this._writeLegacyIpcFormat ? this._write(Int32Array.of(0)) : this._write(Int32Array.of(-1, 0));
      }
      _writeMagic() {
        return this._write(message_js_1.MAGIC);
      }
      _writePadding(nBytes) {
        return nBytes > 0 ? this._write(new Uint8Array(nBytes)) : this;
      }
      _writeRecordBatch(batch) {
        const { byteLength, nodes, bufferRegions, buffers } = vectorassembler_js_1.VectorAssembler.assemble(batch);
        const recordBatch = new metadata.RecordBatch(batch.numRows, nodes, bufferRegions);
        const message = message_js_2.Message.from(recordBatch, byteLength);
        return this._writeDictionaries(batch)._writeMessage(message)._writeBodyBuffers(buffers);
      }
      _writeDictionaryBatch(dictionary, id, isDelta = false) {
        const { byteLength, nodes, bufferRegions, buffers } = vectorassembler_js_1.VectorAssembler.assemble(new vector_js_1.Vector([dictionary]));
        const recordBatch = new metadata.RecordBatch(dictionary.length, nodes, bufferRegions);
        const dictionaryBatch = new metadata.DictionaryBatch(recordBatch, id, isDelta);
        const message = message_js_2.Message.from(dictionaryBatch, byteLength);
        return this._writeMessage(message)._writeBodyBuffers(buffers);
      }
      _writeBodyBuffers(buffers) {
        let buffer;
        let size, padding;
        for (let i = -1, n = buffers.length; ++i < n; ) {
          if ((buffer = buffers[i]) && (size = buffer.byteLength) > 0) {
            this._write(buffer);
            if ((padding = (size + 7 & ~7) - size) > 0) {
              this._writePadding(padding);
            }
          }
        }
        return this;
      }
      _writeDictionaries(batch) {
        var _a, _b;
        for (const [id, dictionary] of batch.dictionaries) {
          const chunks = (_a = dictionary === null || dictionary === void 0 ? void 0 : dictionary.data) !== null && _a !== void 0 ? _a : [];
          const prevDictionary = this._seenDictionaries.get(id);
          const offset = (_b = this._dictionaryDeltaOffsets.get(id)) !== null && _b !== void 0 ? _b : 0;
          if (!prevDictionary || prevDictionary.data[0] !== chunks[0]) {
            for (const [index, chunk] of chunks.entries())
              this._writeDictionaryBatch(chunk, id, index > 0);
          } else if (offset < chunks.length) {
            for (const chunk of chunks.slice(offset))
              this._writeDictionaryBatch(chunk, id, true);
          }
          this._seenDictionaries.set(id, dictionary);
          this._dictionaryDeltaOffsets.set(id, chunks.length);
        }
        return this;
      }
    };
    exports2.RecordBatchWriter = RecordBatchWriter;
    var RecordBatchStreamWriter = class _RecordBatchStreamWriter extends RecordBatchWriter {
      /** @nocollapse */
      static writeAll(input, options) {
        const writer = new _RecordBatchStreamWriter(options);
        if ((0, compat_js_1.isPromise)(input)) {
          return input.then((x) => writer.writeAll(x));
        } else if ((0, compat_js_1.isAsyncIterable)(input)) {
          return writeAllAsync(writer, input);
        }
        return writeAll(writer, input);
      }
    };
    exports2.RecordBatchStreamWriter = RecordBatchStreamWriter;
    var RecordBatchFileWriter = class _RecordBatchFileWriter extends RecordBatchWriter {
      /** @nocollapse */
      static writeAll(input) {
        const writer = new _RecordBatchFileWriter();
        if ((0, compat_js_1.isPromise)(input)) {
          return input.then((x) => writer.writeAll(x));
        } else if ((0, compat_js_1.isAsyncIterable)(input)) {
          return writeAllAsync(writer, input);
        }
        return writeAll(writer, input);
      }
      constructor() {
        super();
        this._autoDestroy = true;
      }
      // @ts-ignore
      _writeSchema(schema) {
        return this._writeMagic()._writePadding(2);
      }
      _writeDictionaryBatch(dictionary, id, isDelta = false) {
        if (!isDelta && this._seenDictionaries.has(id)) {
          throw new Error("The Arrow File format does not support replacement dictionaries. ");
        }
        return super._writeDictionaryBatch(dictionary, id, isDelta);
      }
      _writeFooter(schema) {
        const buffer = file_js_1.Footer.encode(new file_js_1.Footer(schema, enum_js_1.MetadataVersion.V5, this._recordBatchBlocks, this._dictionaryBlocks));
        return super._writeFooter(schema)._write(buffer)._write(Int32Array.of(buffer.byteLength))._writeMagic();
      }
    };
    exports2.RecordBatchFileWriter = RecordBatchFileWriter;
    var RecordBatchJSONWriter = class _RecordBatchJSONWriter extends RecordBatchWriter {
      /** @nocollapse */
      static writeAll(input) {
        return new _RecordBatchJSONWriter().writeAll(input);
      }
      constructor() {
        super();
        this._autoDestroy = true;
        this._recordBatches = [];
        this._recordBatchesWithDictionaries = [];
      }
      _writeMessage() {
        return this;
      }
      // @ts-ignore
      _writeFooter(schema) {
        return this;
      }
      _writeSchema(schema) {
        return this._write(`{
  "schema": ${JSON.stringify({ fields: schema.fields.map((field) => fieldToJSON(field)) }, null, 2)}`);
      }
      _writeDictionaries(batch) {
        if (batch.dictionaries.size > 0) {
          this._recordBatchesWithDictionaries.push(batch);
        }
        return this;
      }
      _writeDictionaryBatch(dictionary, id, isDelta = false) {
        this._write(this._dictionaryBlocks.length === 0 ? `    ` : `,
    `);
        this._write(dictionaryBatchToJSON(dictionary, id, isDelta));
        this._dictionaryBlocks.push(new file_js_1.FileBlock(0, 0, 0));
        return this;
      }
      _writeRecordBatch(batch) {
        this._writeDictionaries(batch);
        this._recordBatches.push(batch);
        return this;
      }
      close() {
        if (this._recordBatchesWithDictionaries.length > 0) {
          this._write(`,
  "dictionaries": [
`);
          for (const batch of this._recordBatchesWithDictionaries) {
            super._writeDictionaries(batch);
          }
          this._write(`
  ]`);
        }
        if (this._recordBatches.length > 0) {
          for (let i = -1, n = this._recordBatches.length; ++i < n; ) {
            this._write(i === 0 ? `,
  "batches": [
    ` : `,
    `);
            this._write(recordBatchToJSON(this._recordBatches[i]));
            this._recordBatchBlocks.push(new file_js_1.FileBlock(0, 0, 0));
          }
          this._write(`
  ]`);
        }
        if (this._schema) {
          this._write(`
}`);
        }
        this._recordBatchesWithDictionaries = [];
        this._recordBatches = [];
        return super.close();
      }
    };
    exports2.RecordBatchJSONWriter = RecordBatchJSONWriter;
    function writeAll(writer, input) {
      let chunks = input;
      if (input instanceof table_js_1.Table) {
        chunks = input.batches;
        writer.reset(void 0, input.schema);
      }
      for (const batch of chunks) {
        writer.write(batch);
      }
      return writer.finish();
    }
    function writeAllAsync(writer, batches) {
      return tslib_1.__awaiter(this, void 0, void 0, function* () {
        var _a, batches_1, batches_1_1;
        var _b, e_1, _c, _d;
        try {
          for (_a = true, batches_1 = tslib_1.__asyncValues(batches); batches_1_1 = yield batches_1.next(), _b = batches_1_1.done, !_b; _a = true) {
            _d = batches_1_1.value;
            _a = false;
            const batch = _d;
            writer.write(batch);
          }
        } catch (e_1_1) {
          e_1 = { error: e_1_1 };
        } finally {
          try {
            if (!_a && !_b && (_c = batches_1.return)) yield _c.call(batches_1);
          } finally {
            if (e_1) throw e_1.error;
          }
        }
        return writer.finish();
      });
    }
    function fieldToJSON({ name, type, nullable }) {
      const assembler = new jsontypeassembler_js_1.JSONTypeAssembler();
      return {
        "name": name,
        "nullable": nullable,
        "type": assembler.visit(type),
        "children": (type.children || []).map((field) => fieldToJSON(field)),
        "dictionary": !type_js_1.DataType.isDictionary(type) ? void 0 : {
          "id": type.id,
          "isOrdered": type.isOrdered,
          "indexType": assembler.visit(type.indices)
        }
      };
    }
    function dictionaryBatchToJSON(dictionary, id, isDelta = false) {
      const [columns] = jsonvectorassembler_js_1.JSONVectorAssembler.assemble(new recordbatch_js_1.RecordBatch({ [id]: dictionary }));
      return JSON.stringify({
        "id": id,
        "isDelta": isDelta,
        "data": {
          "count": dictionary.length,
          "columns": columns
        }
      }, null, 2);
    }
    function recordBatchToJSON(records) {
      const [columns] = jsonvectorassembler_js_1.JSONVectorAssembler.assemble(records);
      return JSON.stringify({
        "count": records.numRows,
        "columns": columns
      }, null, 2);
    }
  }
});

// node_modules/apache-arrow/io/node/iterable.js
var require_iterable = __commonJS({
  "node_modules/apache-arrow/io/node/iterable.js"(exports2) {
    "use strict";
    Object.defineProperty(exports2, "__esModule", { value: true });
    exports2.toNodeStream = void 0;
    var tslib_1 = (init_tslib_es6(), __toCommonJS(tslib_es6_exports));
    var node_stream_1 = require("stream");
    var compat_js_1 = require_compat();
    function toNodeStream(source, options) {
      if ((0, compat_js_1.isAsyncIterable)(source)) {
        return new AsyncIterableReadable(source[Symbol.asyncIterator](), options);
      }
      if ((0, compat_js_1.isIterable)(source)) {
        return new IterableReadable(source[Symbol.iterator](), options);
      }
      throw new Error(`toNodeStream() must be called with an Iterable or AsyncIterable`);
    }
    exports2.toNodeStream = toNodeStream;
    var IterableReadable = class extends node_stream_1.Readable {
      constructor(it, options) {
        super(options);
        this._iterator = it;
        this._pulling = false;
        this._bytesMode = !options || !options.objectMode;
      }
      _read(size) {
        const it = this._iterator;
        if (it && !this._pulling && (this._pulling = true)) {
          this._pulling = this._pull(size, it);
        }
      }
      _destroy(e, cb) {
        const it = this._iterator;
        let fn;
        it && (fn = e != null && it.throw || it.return);
        fn === null || fn === void 0 ? void 0 : fn.call(it, e);
        cb && cb(null);
      }
      _pull(size, it) {
        const bm = this._bytesMode;
        let r = null;
        while (this.readable && !(r = it.next(bm ? size : null)).done) {
          if (size != null) {
            size -= bm && ArrayBuffer.isView(r.value) ? r.value.byteLength : 1;
          }
          if (!this.push(r.value) || size <= 0) {
            break;
          }
        }
        if (((r === null || r === void 0 ? void 0 : r.done) || !this.readable) && (this.push(null) || true)) {
          it.return && it.return();
        }
        return !this.readable;
      }
    };
    var AsyncIterableReadable = class extends node_stream_1.Readable {
      constructor(it, options) {
        super(options);
        this._iterator = it;
        this._pulling = false;
        this._bytesMode = !options || !options.objectMode;
      }
      _read(size) {
        const it = this._iterator;
        if (it && !this._pulling && (this._pulling = true)) {
          (() => tslib_1.__awaiter(this, void 0, void 0, function* () {
            return this._pulling = yield this._pull(size, it);
          }))();
        }
      }
      _destroy(e, cb) {
        const it = this._iterator;
        let fn;
        it && (fn = e != null && it.throw || it.return);
        (fn === null || fn === void 0 ? void 0 : fn.call(it, e).then(() => cb && cb(null))) || cb && cb(null);
      }
      _pull(size, it) {
        return tslib_1.__awaiter(this, void 0, void 0, function* () {
          const bm = this._bytesMode;
          let r = null;
          while (this.readable && !(r = yield it.next(bm ? size : null)).done) {
            if (size != null) {
              size -= bm && ArrayBuffer.isView(r.value) ? r.value.byteLength : 1;
            }
            if (!this.push(r.value) || size <= 0) {
              break;
            }
          }
          if (((r === null || r === void 0 ? void 0 : r.done) || !this.readable) && (this.push(null) || true)) {
            it.return && it.return();
          }
          return !this.readable;
        });
      }
    };
  }
});

// node_modules/apache-arrow/io/node/builder.js
var require_builder2 = __commonJS({
  "node_modules/apache-arrow/io/node/builder.js"(exports2) {
    "use strict";
    Object.defineProperty(exports2, "__esModule", { value: true });
    exports2.builderThroughNodeStream = void 0;
    var node_stream_1 = require("stream");
    var factories_js_1 = require_factories();
    function builderThroughNodeStream(options) {
      return new BuilderDuplex((0, factories_js_1.makeBuilder)(options), options);
    }
    exports2.builderThroughNodeStream = builderThroughNodeStream;
    var BuilderDuplex = class extends node_stream_1.Duplex {
      constructor(builder, options) {
        const { queueingStrategy = "count", autoDestroy = true } = options;
        const { highWaterMark = queueingStrategy !== "bytes" ? 1e3 : Math.pow(2, 14) } = options;
        super({ autoDestroy, highWaterMark: 1, allowHalfOpen: true, writableObjectMode: true, readableObjectMode: true });
        this._numChunks = 0;
        this._finished = false;
        this._builder = builder;
        this._desiredSize = highWaterMark;
        this._getSize = queueingStrategy !== "bytes" ? builderLength : builderByteLength;
      }
      _read(size) {
        this._maybeFlush(this._builder, this._desiredSize = size);
      }
      _final(cb) {
        this._maybeFlush(this._builder.finish(), this._desiredSize);
        cb && cb();
      }
      _write(value, _, cb) {
        const result = this._maybeFlush(this._builder.append(value), this._desiredSize);
        cb && cb();
        return result;
      }
      _destroy(err, cb) {
        this._builder.clear();
        cb && cb(err);
      }
      _maybeFlush(builder, size) {
        if (this._getSize(builder) >= size) {
          ++this._numChunks && this.push(builder.toVector());
        }
        if (builder.finished) {
          if (builder.length > 0 || this._numChunks === 0) {
            ++this._numChunks && this.push(builder.toVector());
          }
          if (!this._finished && (this._finished = true)) {
            this.push(null);
          }
          return false;
        }
        return this._getSize(builder) < this.writableHighWaterMark;
      }
    };
    var builderLength = (builder) => builder.length;
    var builderByteLength = (builder) => builder.byteLength;
  }
});

// node_modules/apache-arrow/io/node/reader.js
var require_reader2 = __commonJS({
  "node_modules/apache-arrow/io/node/reader.js"(exports2) {
    "use strict";
    Object.defineProperty(exports2, "__esModule", { value: true });
    exports2.recordBatchReaderThroughNodeStream = void 0;
    var tslib_1 = (init_tslib_es6(), __toCommonJS(tslib_es6_exports));
    var node_stream_1 = require("stream");
    var stream_js_1 = require_stream();
    var reader_js_1 = require_reader();
    function recordBatchReaderThroughNodeStream(options) {
      return new RecordBatchReaderDuplex(options);
    }
    exports2.recordBatchReaderThroughNodeStream = recordBatchReaderThroughNodeStream;
    var RecordBatchReaderDuplex = class extends node_stream_1.Duplex {
      constructor(options) {
        super(Object.assign(Object.assign({ allowHalfOpen: false }, options), { readableObjectMode: true, writableObjectMode: false }));
        this._pulling = false;
        this._autoDestroy = true;
        this._reader = null;
        this._pulling = false;
        this._asyncQueue = new stream_js_1.AsyncByteQueue();
        this._autoDestroy = options && typeof options.autoDestroy === "boolean" ? options.autoDestroy : true;
      }
      _final(cb) {
        const aq = this._asyncQueue;
        aq === null || aq === void 0 ? void 0 : aq.close();
        cb && cb();
      }
      _write(x, _, cb) {
        const aq = this._asyncQueue;
        aq === null || aq === void 0 ? void 0 : aq.write(x);
        cb && cb();
        return true;
      }
      _read(size) {
        const aq = this._asyncQueue;
        if (aq && !this._pulling && (this._pulling = true)) {
          (() => tslib_1.__awaiter(this, void 0, void 0, function* () {
            if (!this._reader) {
              this._reader = yield this._open(aq);
            }
            this._pulling = yield this._pull(size, this._reader);
          }))();
        }
      }
      _destroy(err, cb) {
        const aq = this._asyncQueue;
        if (aq) {
          err ? aq.abort(err) : aq.close();
        }
        cb(this._asyncQueue = this._reader = null);
      }
      _open(source) {
        return tslib_1.__awaiter(this, void 0, void 0, function* () {
          return yield (yield reader_js_1.RecordBatchReader.from(source)).open({ autoDestroy: this._autoDestroy });
        });
      }
      _pull(size, reader) {
        return tslib_1.__awaiter(this, void 0, void 0, function* () {
          let r = null;
          while (this.readable && !(r = yield reader.next()).done) {
            if (!this.push(r.value) || size != null && --size <= 0) {
              break;
            }
          }
          if (!this.readable || (r === null || r === void 0 ? void 0 : r.done) && (reader.autoDestroy || (yield reader.reset().open()).closed)) {
            this.push(null);
            yield reader.cancel();
          }
          return !this.readable;
        });
      }
    };
  }
});

// node_modules/apache-arrow/io/node/writer.js
var require_writer2 = __commonJS({
  "node_modules/apache-arrow/io/node/writer.js"(exports2) {
    "use strict";
    Object.defineProperty(exports2, "__esModule", { value: true });
    exports2.recordBatchWriterThroughNodeStream = void 0;
    var tslib_1 = (init_tslib_es6(), __toCommonJS(tslib_es6_exports));
    var node_stream_1 = require("stream");
    var stream_js_1 = require_stream();
    function recordBatchWriterThroughNodeStream(options) {
      return new RecordBatchWriterDuplex(new this(options));
    }
    exports2.recordBatchWriterThroughNodeStream = recordBatchWriterThroughNodeStream;
    var RecordBatchWriterDuplex = class extends node_stream_1.Duplex {
      constructor(writer, options) {
        super(Object.assign(Object.assign({ allowHalfOpen: false }, options), { writableObjectMode: true, readableObjectMode: false }));
        this._pulling = false;
        this._writer = writer;
        this._reader = new stream_js_1.AsyncByteStream(writer);
      }
      _final(cb) {
        const writer = this._writer;
        writer === null || writer === void 0 ? void 0 : writer.close();
        cb && cb();
      }
      _write(x, _, cb) {
        const writer = this._writer;
        writer === null || writer === void 0 ? void 0 : writer.write(x);
        cb && cb();
        return true;
      }
      _read(size) {
        const it = this._reader;
        if (it && !this._pulling && (this._pulling = true)) {
          (() => tslib_1.__awaiter(this, void 0, void 0, function* () {
            return this._pulling = yield this._pull(size, it);
          }))();
        }
      }
      _destroy(err, cb) {
        const writer = this._writer;
        if (writer) {
          err ? writer.abort(err) : writer.close();
        }
        cb(this._reader = this._writer = null);
      }
      _pull(size, reader) {
        return tslib_1.__awaiter(this, void 0, void 0, function* () {
          let r = null;
          while (this.readable && !(r = yield reader.next(size || null)).done) {
            if (size != null && r.value) {
              size -= r.value.byteLength;
            }
            if (!this.push(r.value) || size <= 0) {
              break;
            }
          }
          if ((r === null || r === void 0 ? void 0 : r.done) || !this.readable) {
            this.push(null);
            yield reader.cancel();
          }
          return !this.readable;
        });
      }
    };
  }
});

// node_modules/apache-arrow/io/whatwg/iterable.js
var require_iterable2 = __commonJS({
  "node_modules/apache-arrow/io/whatwg/iterable.js"(exports2) {
    "use strict";
    Object.defineProperty(exports2, "__esModule", { value: true });
    exports2.toDOMStream = void 0;
    var tslib_1 = (init_tslib_es6(), __toCommonJS(tslib_es6_exports));
    var buffer_js_1 = require_buffer();
    var compat_js_1 = require_compat();
    function toDOMStream(source, options) {
      if ((0, compat_js_1.isAsyncIterable)(source)) {
        return asyncIterableAsReadableDOMStream(source, options);
      }
      if ((0, compat_js_1.isIterable)(source)) {
        return iterableAsReadableDOMStream(source, options);
      }
      throw new Error(`toDOMStream() must be called with an Iterable or AsyncIterable`);
    }
    exports2.toDOMStream = toDOMStream;
    function iterableAsReadableDOMStream(source, options) {
      let it = null;
      const bm = (options === null || options === void 0 ? void 0 : options.type) === "bytes" || false;
      const hwm = (options === null || options === void 0 ? void 0 : options.highWaterMark) || Math.pow(2, 24);
      return new ReadableStream(Object.assign(Object.assign({}, options), {
        start(controller) {
          next(controller, it || (it = source[Symbol.iterator]()));
        },
        pull(controller) {
          it ? next(controller, it) : controller.close();
        },
        cancel() {
          ((it === null || it === void 0 ? void 0 : it.return) && it.return() || true) && (it = null);
        }
      }), Object.assign({ highWaterMark: bm ? hwm : void 0 }, options));
      function next(controller, it2) {
        let buf;
        let r = null;
        let size = controller.desiredSize || null;
        while (!(r = it2.next(bm ? size : null)).done) {
          if (ArrayBuffer.isView(r.value) && (buf = (0, buffer_js_1.toUint8Array)(r.value))) {
            size != null && bm && (size = size - buf.byteLength + 1);
            r.value = buf;
          }
          controller.enqueue(r.value);
          if (size != null && --size <= 0) {
            return;
          }
        }
        controller.close();
      }
    }
    function asyncIterableAsReadableDOMStream(source, options) {
      let it = null;
      const bm = (options === null || options === void 0 ? void 0 : options.type) === "bytes" || false;
      const hwm = (options === null || options === void 0 ? void 0 : options.highWaterMark) || Math.pow(2, 24);
      return new ReadableStream(Object.assign(Object.assign({}, options), {
        start(controller) {
          return tslib_1.__awaiter(this, void 0, void 0, function* () {
            yield next(controller, it || (it = source[Symbol.asyncIterator]()));
          });
        },
        pull(controller) {
          return tslib_1.__awaiter(this, void 0, void 0, function* () {
            it ? yield next(controller, it) : controller.close();
          });
        },
        cancel() {
          return tslib_1.__awaiter(this, void 0, void 0, function* () {
            ((it === null || it === void 0 ? void 0 : it.return) && (yield it.return()) || true) && (it = null);
          });
        }
      }), Object.assign({ highWaterMark: bm ? hwm : void 0 }, options));
      function next(controller, it2) {
        return tslib_1.__awaiter(this, void 0, void 0, function* () {
          let buf;
          let r = null;
          let size = controller.desiredSize || null;
          while (!(r = yield it2.next(bm ? size : null)).done) {
            if (ArrayBuffer.isView(r.value) && (buf = (0, buffer_js_1.toUint8Array)(r.value))) {
              size != null && bm && (size = size - buf.byteLength + 1);
              r.value = buf;
            }
            controller.enqueue(r.value);
            if (size != null && --size <= 0) {
              return;
            }
          }
          controller.close();
        });
      }
    }
  }
});

// node_modules/apache-arrow/io/whatwg/builder.js
var require_builder3 = __commonJS({
  "node_modules/apache-arrow/io/whatwg/builder.js"(exports2) {
    "use strict";
    Object.defineProperty(exports2, "__esModule", { value: true });
    exports2.BuilderTransform = exports2.builderThroughDOMStream = void 0;
    var tslib_1 = (init_tslib_es6(), __toCommonJS(tslib_es6_exports));
    var factories_js_1 = require_factories();
    function builderThroughDOMStream(options) {
      return new BuilderTransform(options);
    }
    exports2.builderThroughDOMStream = builderThroughDOMStream;
    var BuilderTransform = class {
      constructor(options) {
        this._numChunks = 0;
        this._finished = false;
        this._bufferedSize = 0;
        const { ["readableStrategy"]: readableStrategy, ["writableStrategy"]: writableStrategy, ["queueingStrategy"]: queueingStrategy = "count" } = options, builderOptions = tslib_1.__rest(options, ["readableStrategy", "writableStrategy", "queueingStrategy"]);
        this._controller = null;
        this._builder = (0, factories_js_1.makeBuilder)(builderOptions);
        this._getSize = queueingStrategy !== "bytes" ? chunkLength : chunkByteLength;
        const { ["highWaterMark"]: readableHighWaterMark = queueingStrategy === "bytes" ? Math.pow(2, 14) : 1e3 } = Object.assign({}, readableStrategy);
        const { ["highWaterMark"]: writableHighWaterMark = queueingStrategy === "bytes" ? Math.pow(2, 14) : 1e3 } = Object.assign({}, writableStrategy);
        this["readable"] = new ReadableStream({
          ["cancel"]: () => {
            this._builder.clear();
          },
          ["pull"]: (c) => {
            this._maybeFlush(this._builder, this._controller = c);
          },
          ["start"]: (c) => {
            this._maybeFlush(this._builder, this._controller = c);
          }
        }, {
          "highWaterMark": readableHighWaterMark,
          "size": queueingStrategy !== "bytes" ? chunkLength : chunkByteLength
        });
        this["writable"] = new WritableStream({
          ["abort"]: () => {
            this._builder.clear();
          },
          ["write"]: () => {
            this._maybeFlush(this._builder, this._controller);
          },
          ["close"]: () => {
            this._maybeFlush(this._builder.finish(), this._controller);
          }
        }, {
          "highWaterMark": writableHighWaterMark,
          "size": (value) => this._writeValueAndReturnChunkSize(value)
        });
      }
      _writeValueAndReturnChunkSize(value) {
        const bufferedSize = this._bufferedSize;
        this._bufferedSize = this._getSize(this._builder.append(value));
        return this._bufferedSize - bufferedSize;
      }
      _maybeFlush(builder, controller) {
        if (controller == null) {
          return;
        }
        if (this._bufferedSize >= controller.desiredSize) {
          ++this._numChunks && this._enqueue(controller, builder.toVector());
        }
        if (builder.finished) {
          if (builder.length > 0 || this._numChunks === 0) {
            ++this._numChunks && this._enqueue(controller, builder.toVector());
          }
          if (!this._finished && (this._finished = true)) {
            this._enqueue(controller, null);
          }
        }
      }
      _enqueue(controller, chunk) {
        this._bufferedSize = 0;
        this._controller = null;
        chunk == null ? controller.close() : controller.enqueue(chunk);
      }
    };
    exports2.BuilderTransform = BuilderTransform;
    var chunkLength = (chunk) => {
      var _a;
      return (_a = chunk === null || chunk === void 0 ? void 0 : chunk.length) !== null && _a !== void 0 ? _a : 0;
    };
    var chunkByteLength = (chunk) => {
      var _a;
      return (_a = chunk === null || chunk === void 0 ? void 0 : chunk.byteLength) !== null && _a !== void 0 ? _a : 0;
    };
  }
});

// node_modules/apache-arrow/io/whatwg/reader.js
var require_reader3 = __commonJS({
  "node_modules/apache-arrow/io/whatwg/reader.js"(exports2) {
    "use strict";
    Object.defineProperty(exports2, "__esModule", { value: true });
    exports2.recordBatchReaderThroughDOMStream = void 0;
    var tslib_1 = (init_tslib_es6(), __toCommonJS(tslib_es6_exports));
    var stream_js_1 = require_stream();
    var reader_js_1 = require_reader();
    function recordBatchReaderThroughDOMStream(writableStrategy, readableStrategy) {
      const queue = new stream_js_1.AsyncByteQueue();
      let reader = null;
      const readable = new ReadableStream({
        cancel() {
          return tslib_1.__awaiter(this, void 0, void 0, function* () {
            yield queue.close();
          });
        },
        start(controller) {
          return tslib_1.__awaiter(this, void 0, void 0, function* () {
            yield next(controller, reader || (reader = yield open()));
          });
        },
        pull(controller) {
          return tslib_1.__awaiter(this, void 0, void 0, function* () {
            reader ? yield next(controller, reader) : controller.close();
          });
        }
      });
      return { writable: new WritableStream(queue, Object.assign({ "highWaterMark": Math.pow(2, 14) }, writableStrategy)), readable };
      function open() {
        return tslib_1.__awaiter(this, void 0, void 0, function* () {
          return yield (yield reader_js_1.RecordBatchReader.from(queue)).open(readableStrategy);
        });
      }
      function next(controller, reader2) {
        return tslib_1.__awaiter(this, void 0, void 0, function* () {
          let size = controller.desiredSize;
          let r = null;
          while (!(r = yield reader2.next()).done) {
            controller.enqueue(r.value);
            if (size != null && --size <= 0) {
              return;
            }
          }
          controller.close();
        });
      }
    }
    exports2.recordBatchReaderThroughDOMStream = recordBatchReaderThroughDOMStream;
  }
});

// node_modules/apache-arrow/io/whatwg/writer.js
var require_writer3 = __commonJS({
  "node_modules/apache-arrow/io/whatwg/writer.js"(exports2) {
    "use strict";
    Object.defineProperty(exports2, "__esModule", { value: true });
    exports2.recordBatchWriterThroughDOMStream = void 0;
    var tslib_1 = (init_tslib_es6(), __toCommonJS(tslib_es6_exports));
    var stream_js_1 = require_stream();
    function recordBatchWriterThroughDOMStream(writableStrategy, readableStrategy) {
      const writer = new this(writableStrategy);
      const reader = new stream_js_1.AsyncByteStream(writer);
      const readable = new ReadableStream({
        // type: 'bytes',
        cancel() {
          return tslib_1.__awaiter(this, void 0, void 0, function* () {
            yield reader.cancel();
          });
        },
        pull(controller) {
          return tslib_1.__awaiter(this, void 0, void 0, function* () {
            yield next(controller);
          });
        },
        start(controller) {
          return tslib_1.__awaiter(this, void 0, void 0, function* () {
            yield next(controller);
          });
        }
      }, Object.assign({ "highWaterMark": Math.pow(2, 14) }, readableStrategy));
      return { writable: new WritableStream(writer, writableStrategy), readable };
      function next(controller) {
        return tslib_1.__awaiter(this, void 0, void 0, function* () {
          let buf = null;
          let size = controller.desiredSize;
          while (buf = yield reader.read(size || null)) {
            controller.enqueue(buf);
            if (size != null && (size -= buf.byteLength) <= 0) {
              return;
            }
          }
          controller.close();
        });
      }
    }
    exports2.recordBatchWriterThroughDOMStream = recordBatchWriterThroughDOMStream;
  }
});

// node_modules/apache-arrow/ipc/serialization.js
var require_serialization = __commonJS({
  "node_modules/apache-arrow/ipc/serialization.js"(exports2) {
    "use strict";
    Object.defineProperty(exports2, "__esModule", { value: true });
    exports2.tableToIPC = exports2.tableFromIPC = void 0;
    var table_js_1 = require_table();
    var compat_js_1 = require_compat();
    var reader_js_1 = require_reader();
    var writer_js_1 = require_writer();
    function tableFromIPC(input) {
      const reader = reader_js_1.RecordBatchReader.from(input);
      if ((0, compat_js_1.isPromise)(reader)) {
        return reader.then((reader2) => tableFromIPC(reader2));
      }
      if (reader.isAsync()) {
        return reader.readAll().then((xs) => new table_js_1.Table(xs));
      }
      return new table_js_1.Table(reader.readAll());
    }
    exports2.tableFromIPC = tableFromIPC;
    function tableToIPC(table, type = "stream") {
      return (type === "stream" ? writer_js_1.RecordBatchStreamWriter : writer_js_1.RecordBatchFileWriter).writeAll(table).toUint8Array(true);
    }
    exports2.tableToIPC = tableToIPC;
  }
});

// node_modules/apache-arrow/Arrow.js
var require_Arrow = __commonJS({
  "node_modules/apache-arrow/Arrow.js"(exports2) {
    "use strict";
    Object.defineProperty(exports2, "__esModule", { value: true });
    exports2.DenseUnion = exports2.Union = exports2.Struct = exports2.List = exports2.Decimal = exports2.TimeNanosecond = exports2.TimeMicrosecond = exports2.TimeMillisecond = exports2.TimeSecond = exports2.Time = exports2.TimestampNanosecond = exports2.TimestampMicrosecond = exports2.TimestampMillisecond = exports2.TimestampSecond = exports2.Timestamp = exports2.DateMillisecond = exports2.DateDay = exports2.Date_ = exports2.FixedSizeBinary = exports2.LargeBinary = exports2.Binary = exports2.LargeUtf8 = exports2.Utf8 = exports2.Float64 = exports2.Float32 = exports2.Float16 = exports2.Float = exports2.Uint64 = exports2.Uint32 = exports2.Uint16 = exports2.Uint8 = exports2.Int64 = exports2.Int32 = exports2.Int16 = exports2.Int8 = exports2.Int = exports2.Bool = exports2.Null = exports2.DataType = exports2.makeData = exports2.Data = exports2.MetadataVersion = exports2.IntervalUnit = exports2.UnionMode = exports2.Precision = exports2.TimeUnit = exports2.DateUnit = exports2.BufferType = exports2.Type = exports2.MessageHeader = void 0;
    exports2.TimeBuilder = exports2.Uint64Builder = exports2.Uint32Builder = exports2.Uint16Builder = exports2.Uint8Builder = exports2.Int64Builder = exports2.Int32Builder = exports2.Int16Builder = exports2.Int8Builder = exports2.IntBuilder = exports2.Float64Builder = exports2.Float32Builder = exports2.Float16Builder = exports2.FloatBuilder = exports2.FixedSizeBinaryBuilder = exports2.DictionaryBuilder = exports2.DecimalBuilder = exports2.DateMillisecondBuilder = exports2.DateDayBuilder = exports2.DateBuilder = exports2.NullBuilder = exports2.BoolBuilder = exports2.builderThroughAsyncIterable = exports2.builderThroughIterable = exports2.tableFromJSON = exports2.vectorFromArray = exports2.makeBuilder = exports2.Builder = exports2.StructRow = exports2.MapRow = exports2.Field = exports2.Schema = exports2.Visitor = exports2.makeVector = exports2.Vector = exports2.tableFromArrays = exports2.makeTable = exports2.Table = exports2.Map_ = exports2.FixedSizeList = exports2.DurationNanosecond = exports2.DurationMicrosecond = exports2.DurationMillisecond = exports2.DurationSecond = exports2.Duration = exports2.IntervalYearMonth = exports2.IntervalDayTime = exports2.Interval = exports2.Dictionary = exports2.SparseUnion = void 0;
    exports2.util = exports2.RecordBatch = exports2.Message = exports2.JSONMessageReader = exports2.AsyncMessageReader = exports2.MessageReader = exports2.tableFromIPC = exports2.tableToIPC = exports2.RecordBatchJSONWriter = exports2.RecordBatchStreamWriter = exports2.RecordBatchFileWriter = exports2.RecordBatchWriter = exports2.AsyncRecordBatchStreamReader = exports2.AsyncRecordBatchFileReader = exports2.RecordBatchStreamReader = exports2.RecordBatchFileReader = exports2.RecordBatchReader = exports2.AsyncByteQueue = exports2.AsyncByteStream = exports2.ByteStream = exports2.DenseUnionBuilder = exports2.SparseUnionBuilder = exports2.UnionBuilder = exports2.StructBuilder = exports2.MapBuilder = exports2.FixedSizeListBuilder = exports2.ListBuilder = exports2.LargeBinaryBuilder = exports2.BinaryBuilder = exports2.LargeUtf8Builder = exports2.Utf8Builder = exports2.DurationNanosecondBuilder = exports2.DurationMicrosecondBuilder = exports2.DurationMillisecondBuilder = exports2.DurationSecondBuilder = exports2.DurationBuilder = exports2.IntervalYearMonthBuilder = exports2.IntervalDayTimeBuilder = exports2.IntervalBuilder = exports2.TimestampNanosecondBuilder = exports2.TimestampMicrosecondBuilder = exports2.TimestampMillisecondBuilder = exports2.TimestampSecondBuilder = exports2.TimestampBuilder = exports2.TimeNanosecondBuilder = exports2.TimeMicrosecondBuilder = exports2.TimeMillisecondBuilder = exports2.TimeSecondBuilder = void 0;
    var message_header_js_1 = require_message_header();
    Object.defineProperty(exports2, "MessageHeader", { enumerable: true, get: function() {
      return message_header_js_1.MessageHeader;
    } });
    var enum_js_1 = require_enum();
    Object.defineProperty(exports2, "Type", { enumerable: true, get: function() {
      return enum_js_1.Type;
    } });
    Object.defineProperty(exports2, "BufferType", { enumerable: true, get: function() {
      return enum_js_1.BufferType;
    } });
    Object.defineProperty(exports2, "DateUnit", { enumerable: true, get: function() {
      return enum_js_1.DateUnit;
    } });
    Object.defineProperty(exports2, "TimeUnit", { enumerable: true, get: function() {
      return enum_js_1.TimeUnit;
    } });
    Object.defineProperty(exports2, "Precision", { enumerable: true, get: function() {
      return enum_js_1.Precision;
    } });
    Object.defineProperty(exports2, "UnionMode", { enumerable: true, get: function() {
      return enum_js_1.UnionMode;
    } });
    Object.defineProperty(exports2, "IntervalUnit", { enumerable: true, get: function() {
      return enum_js_1.IntervalUnit;
    } });
    Object.defineProperty(exports2, "MetadataVersion", { enumerable: true, get: function() {
      return enum_js_1.MetadataVersion;
    } });
    var data_js_1 = require_data();
    Object.defineProperty(exports2, "Data", { enumerable: true, get: function() {
      return data_js_1.Data;
    } });
    Object.defineProperty(exports2, "makeData", { enumerable: true, get: function() {
      return data_js_1.makeData;
    } });
    var type_js_1 = require_type2();
    Object.defineProperty(exports2, "DataType", { enumerable: true, get: function() {
      return type_js_1.DataType;
    } });
    Object.defineProperty(exports2, "Null", { enumerable: true, get: function() {
      return type_js_1.Null;
    } });
    Object.defineProperty(exports2, "Bool", { enumerable: true, get: function() {
      return type_js_1.Bool;
    } });
    Object.defineProperty(exports2, "Int", { enumerable: true, get: function() {
      return type_js_1.Int;
    } });
    Object.defineProperty(exports2, "Int8", { enumerable: true, get: function() {
      return type_js_1.Int8;
    } });
    Object.defineProperty(exports2, "Int16", { enumerable: true, get: function() {
      return type_js_1.Int16;
    } });
    Object.defineProperty(exports2, "Int32", { enumerable: true, get: function() {
      return type_js_1.Int32;
    } });
    Object.defineProperty(exports2, "Int64", { enumerable: true, get: function() {
      return type_js_1.Int64;
    } });
    Object.defineProperty(exports2, "Uint8", { enumerable: true, get: function() {
      return type_js_1.Uint8;
    } });
    Object.defineProperty(exports2, "Uint16", { enumerable: true, get: function() {
      return type_js_1.Uint16;
    } });
    Object.defineProperty(exports2, "Uint32", { enumerable: true, get: function() {
      return type_js_1.Uint32;
    } });
    Object.defineProperty(exports2, "Uint64", { enumerable: true, get: function() {
      return type_js_1.Uint64;
    } });
    Object.defineProperty(exports2, "Float", { enumerable: true, get: function() {
      return type_js_1.Float;
    } });
    Object.defineProperty(exports2, "Float16", { enumerable: true, get: function() {
      return type_js_1.Float16;
    } });
    Object.defineProperty(exports2, "Float32", { enumerable: true, get: function() {
      return type_js_1.Float32;
    } });
    Object.defineProperty(exports2, "Float64", { enumerable: true, get: function() {
      return type_js_1.Float64;
    } });
    Object.defineProperty(exports2, "Utf8", { enumerable: true, get: function() {
      return type_js_1.Utf8;
    } });
    Object.defineProperty(exports2, "LargeUtf8", { enumerable: true, get: function() {
      return type_js_1.LargeUtf8;
    } });
    Object.defineProperty(exports2, "Binary", { enumerable: true, get: function() {
      return type_js_1.Binary;
    } });
    Object.defineProperty(exports2, "LargeBinary", { enumerable: true, get: function() {
      return type_js_1.LargeBinary;
    } });
    Object.defineProperty(exports2, "FixedSizeBinary", { enumerable: true, get: function() {
      return type_js_1.FixedSizeBinary;
    } });
    Object.defineProperty(exports2, "Date_", { enumerable: true, get: function() {
      return type_js_1.Date_;
    } });
    Object.defineProperty(exports2, "DateDay", { enumerable: true, get: function() {
      return type_js_1.DateDay;
    } });
    Object.defineProperty(exports2, "DateMillisecond", { enumerable: true, get: function() {
      return type_js_1.DateMillisecond;
    } });
    Object.defineProperty(exports2, "Timestamp", { enumerable: true, get: function() {
      return type_js_1.Timestamp;
    } });
    Object.defineProperty(exports2, "TimestampSecond", { enumerable: true, get: function() {
      return type_js_1.TimestampSecond;
    } });
    Object.defineProperty(exports2, "TimestampMillisecond", { enumerable: true, get: function() {
      return type_js_1.TimestampMillisecond;
    } });
    Object.defineProperty(exports2, "TimestampMicrosecond", { enumerable: true, get: function() {
      return type_js_1.TimestampMicrosecond;
    } });
    Object.defineProperty(exports2, "TimestampNanosecond", { enumerable: true, get: function() {
      return type_js_1.TimestampNanosecond;
    } });
    Object.defineProperty(exports2, "Time", { enumerable: true, get: function() {
      return type_js_1.Time;
    } });
    Object.defineProperty(exports2, "TimeSecond", { enumerable: true, get: function() {
      return type_js_1.TimeSecond;
    } });
    Object.defineProperty(exports2, "TimeMillisecond", { enumerable: true, get: function() {
      return type_js_1.TimeMillisecond;
    } });
    Object.defineProperty(exports2, "TimeMicrosecond", { enumerable: true, get: function() {
      return type_js_1.TimeMicrosecond;
    } });
    Object.defineProperty(exports2, "TimeNanosecond", { enumerable: true, get: function() {
      return type_js_1.TimeNanosecond;
    } });
    Object.defineProperty(exports2, "Decimal", { enumerable: true, get: function() {
      return type_js_1.Decimal;
    } });
    Object.defineProperty(exports2, "List", { enumerable: true, get: function() {
      return type_js_1.List;
    } });
    Object.defineProperty(exports2, "Struct", { enumerable: true, get: function() {
      return type_js_1.Struct;
    } });
    Object.defineProperty(exports2, "Union", { enumerable: true, get: function() {
      return type_js_1.Union;
    } });
    Object.defineProperty(exports2, "DenseUnion", { enumerable: true, get: function() {
      return type_js_1.DenseUnion;
    } });
    Object.defineProperty(exports2, "SparseUnion", { enumerable: true, get: function() {
      return type_js_1.SparseUnion;
    } });
    Object.defineProperty(exports2, "Dictionary", { enumerable: true, get: function() {
      return type_js_1.Dictionary;
    } });
    Object.defineProperty(exports2, "Interval", { enumerable: true, get: function() {
      return type_js_1.Interval;
    } });
    Object.defineProperty(exports2, "IntervalDayTime", { enumerable: true, get: function() {
      return type_js_1.IntervalDayTime;
    } });
    Object.defineProperty(exports2, "IntervalYearMonth", { enumerable: true, get: function() {
      return type_js_1.IntervalYearMonth;
    } });
    Object.defineProperty(exports2, "Duration", { enumerable: true, get: function() {
      return type_js_1.Duration;
    } });
    Object.defineProperty(exports2, "DurationSecond", { enumerable: true, get: function() {
      return type_js_1.DurationSecond;
    } });
    Object.defineProperty(exports2, "DurationMillisecond", { enumerable: true, get: function() {
      return type_js_1.DurationMillisecond;
    } });
    Object.defineProperty(exports2, "DurationMicrosecond", { enumerable: true, get: function() {
      return type_js_1.DurationMicrosecond;
    } });
    Object.defineProperty(exports2, "DurationNanosecond", { enumerable: true, get: function() {
      return type_js_1.DurationNanosecond;
    } });
    Object.defineProperty(exports2, "FixedSizeList", { enumerable: true, get: function() {
      return type_js_1.FixedSizeList;
    } });
    Object.defineProperty(exports2, "Map_", { enumerable: true, get: function() {
      return type_js_1.Map_;
    } });
    var table_js_1 = require_table();
    Object.defineProperty(exports2, "Table", { enumerable: true, get: function() {
      return table_js_1.Table;
    } });
    Object.defineProperty(exports2, "makeTable", { enumerable: true, get: function() {
      return table_js_1.makeTable;
    } });
    Object.defineProperty(exports2, "tableFromArrays", { enumerable: true, get: function() {
      return table_js_1.tableFromArrays;
    } });
    var vector_js_1 = require_vector2();
    Object.defineProperty(exports2, "Vector", { enumerable: true, get: function() {
      return vector_js_1.Vector;
    } });
    Object.defineProperty(exports2, "makeVector", { enumerable: true, get: function() {
      return vector_js_1.makeVector;
    } });
    var visitor_js_1 = require_visitor();
    Object.defineProperty(exports2, "Visitor", { enumerable: true, get: function() {
      return visitor_js_1.Visitor;
    } });
    var schema_js_1 = require_schema2();
    Object.defineProperty(exports2, "Schema", { enumerable: true, get: function() {
      return schema_js_1.Schema;
    } });
    Object.defineProperty(exports2, "Field", { enumerable: true, get: function() {
      return schema_js_1.Field;
    } });
    var map_js_1 = require_map2();
    Object.defineProperty(exports2, "MapRow", { enumerable: true, get: function() {
      return map_js_1.MapRow;
    } });
    var struct_js_1 = require_struct2();
    Object.defineProperty(exports2, "StructRow", { enumerable: true, get: function() {
      return struct_js_1.StructRow;
    } });
    var builder_js_1 = require_builder();
    Object.defineProperty(exports2, "Builder", { enumerable: true, get: function() {
      return builder_js_1.Builder;
    } });
    var factories_js_1 = require_factories();
    Object.defineProperty(exports2, "makeBuilder", { enumerable: true, get: function() {
      return factories_js_1.makeBuilder;
    } });
    Object.defineProperty(exports2, "vectorFromArray", { enumerable: true, get: function() {
      return factories_js_1.vectorFromArray;
    } });
    Object.defineProperty(exports2, "tableFromJSON", { enumerable: true, get: function() {
      return factories_js_1.tableFromJSON;
    } });
    Object.defineProperty(exports2, "builderThroughIterable", { enumerable: true, get: function() {
      return factories_js_1.builderThroughIterable;
    } });
    Object.defineProperty(exports2, "builderThroughAsyncIterable", { enumerable: true, get: function() {
      return factories_js_1.builderThroughAsyncIterable;
    } });
    var bool_js_1 = require_bool2();
    Object.defineProperty(exports2, "BoolBuilder", { enumerable: true, get: function() {
      return bool_js_1.BoolBuilder;
    } });
    var null_js_1 = require_null2();
    Object.defineProperty(exports2, "NullBuilder", { enumerable: true, get: function() {
      return null_js_1.NullBuilder;
    } });
    var date_js_1 = require_date2();
    Object.defineProperty(exports2, "DateBuilder", { enumerable: true, get: function() {
      return date_js_1.DateBuilder;
    } });
    Object.defineProperty(exports2, "DateDayBuilder", { enumerable: true, get: function() {
      return date_js_1.DateDayBuilder;
    } });
    Object.defineProperty(exports2, "DateMillisecondBuilder", { enumerable: true, get: function() {
      return date_js_1.DateMillisecondBuilder;
    } });
    var decimal_js_1 = require_decimal2();
    Object.defineProperty(exports2, "DecimalBuilder", { enumerable: true, get: function() {
      return decimal_js_1.DecimalBuilder;
    } });
    var dictionary_js_1 = require_dictionary();
    Object.defineProperty(exports2, "DictionaryBuilder", { enumerable: true, get: function() {
      return dictionary_js_1.DictionaryBuilder;
    } });
    var fixedsizebinary_js_1 = require_fixedsizebinary();
    Object.defineProperty(exports2, "FixedSizeBinaryBuilder", { enumerable: true, get: function() {
      return fixedsizebinary_js_1.FixedSizeBinaryBuilder;
    } });
    var float_js_1 = require_float();
    Object.defineProperty(exports2, "FloatBuilder", { enumerable: true, get: function() {
      return float_js_1.FloatBuilder;
    } });
    Object.defineProperty(exports2, "Float16Builder", { enumerable: true, get: function() {
      return float_js_1.Float16Builder;
    } });
    Object.defineProperty(exports2, "Float32Builder", { enumerable: true, get: function() {
      return float_js_1.Float32Builder;
    } });
    Object.defineProperty(exports2, "Float64Builder", { enumerable: true, get: function() {
      return float_js_1.Float64Builder;
    } });
    var int_js_1 = require_int3();
    Object.defineProperty(exports2, "IntBuilder", { enumerable: true, get: function() {
      return int_js_1.IntBuilder;
    } });
    Object.defineProperty(exports2, "Int8Builder", { enumerable: true, get: function() {
      return int_js_1.Int8Builder;
    } });
    Object.defineProperty(exports2, "Int16Builder", { enumerable: true, get: function() {
      return int_js_1.Int16Builder;
    } });
    Object.defineProperty(exports2, "Int32Builder", { enumerable: true, get: function() {
      return int_js_1.Int32Builder;
    } });
    Object.defineProperty(exports2, "Int64Builder", { enumerable: true, get: function() {
      return int_js_1.Int64Builder;
    } });
    Object.defineProperty(exports2, "Uint8Builder", { enumerable: true, get: function() {
      return int_js_1.Uint8Builder;
    } });
    Object.defineProperty(exports2, "Uint16Builder", { enumerable: true, get: function() {
      return int_js_1.Uint16Builder;
    } });
    Object.defineProperty(exports2, "Uint32Builder", { enumerable: true, get: function() {
      return int_js_1.Uint32Builder;
    } });
    Object.defineProperty(exports2, "Uint64Builder", { enumerable: true, get: function() {
      return int_js_1.Uint64Builder;
    } });
    var time_js_1 = require_time2();
    Object.defineProperty(exports2, "TimeBuilder", { enumerable: true, get: function() {
      return time_js_1.TimeBuilder;
    } });
    Object.defineProperty(exports2, "TimeSecondBuilder", { enumerable: true, get: function() {
      return time_js_1.TimeSecondBuilder;
    } });
    Object.defineProperty(exports2, "TimeMillisecondBuilder", { enumerable: true, get: function() {
      return time_js_1.TimeMillisecondBuilder;
    } });
    Object.defineProperty(exports2, "TimeMicrosecondBuilder", { enumerable: true, get: function() {
      return time_js_1.TimeMicrosecondBuilder;
    } });
    Object.defineProperty(exports2, "TimeNanosecondBuilder", { enumerable: true, get: function() {
      return time_js_1.TimeNanosecondBuilder;
    } });
    var timestamp_js_1 = require_timestamp2();
    Object.defineProperty(exports2, "TimestampBuilder", { enumerable: true, get: function() {
      return timestamp_js_1.TimestampBuilder;
    } });
    Object.defineProperty(exports2, "TimestampSecondBuilder", { enumerable: true, get: function() {
      return timestamp_js_1.TimestampSecondBuilder;
    } });
    Object.defineProperty(exports2, "TimestampMillisecondBuilder", { enumerable: true, get: function() {
      return timestamp_js_1.TimestampMillisecondBuilder;
    } });
    Object.defineProperty(exports2, "TimestampMicrosecondBuilder", { enumerable: true, get: function() {
      return timestamp_js_1.TimestampMicrosecondBuilder;
    } });
    Object.defineProperty(exports2, "TimestampNanosecondBuilder", { enumerable: true, get: function() {
      return timestamp_js_1.TimestampNanosecondBuilder;
    } });
    var interval_js_1 = require_interval2();
    Object.defineProperty(exports2, "IntervalBuilder", { enumerable: true, get: function() {
      return interval_js_1.IntervalBuilder;
    } });
    Object.defineProperty(exports2, "IntervalDayTimeBuilder", { enumerable: true, get: function() {
      return interval_js_1.IntervalDayTimeBuilder;
    } });
    Object.defineProperty(exports2, "IntervalYearMonthBuilder", { enumerable: true, get: function() {
      return interval_js_1.IntervalYearMonthBuilder;
    } });
    var duration_js_1 = require_duration2();
    Object.defineProperty(exports2, "DurationBuilder", { enumerable: true, get: function() {
      return duration_js_1.DurationBuilder;
    } });
    Object.defineProperty(exports2, "DurationSecondBuilder", { enumerable: true, get: function() {
      return duration_js_1.DurationSecondBuilder;
    } });
    Object.defineProperty(exports2, "DurationMillisecondBuilder", { enumerable: true, get: function() {
      return duration_js_1.DurationMillisecondBuilder;
    } });
    Object.defineProperty(exports2, "DurationMicrosecondBuilder", { enumerable: true, get: function() {
      return duration_js_1.DurationMicrosecondBuilder;
    } });
    Object.defineProperty(exports2, "DurationNanosecondBuilder", { enumerable: true, get: function() {
      return duration_js_1.DurationNanosecondBuilder;
    } });
    var utf8_js_1 = require_utf83();
    Object.defineProperty(exports2, "Utf8Builder", { enumerable: true, get: function() {
      return utf8_js_1.Utf8Builder;
    } });
    var largeutf8_js_1 = require_largeutf8();
    Object.defineProperty(exports2, "LargeUtf8Builder", { enumerable: true, get: function() {
      return largeutf8_js_1.LargeUtf8Builder;
    } });
    var binary_js_1 = require_binary2();
    Object.defineProperty(exports2, "BinaryBuilder", { enumerable: true, get: function() {
      return binary_js_1.BinaryBuilder;
    } });
    var largebinary_js_1 = require_largebinary();
    Object.defineProperty(exports2, "LargeBinaryBuilder", { enumerable: true, get: function() {
      return largebinary_js_1.LargeBinaryBuilder;
    } });
    var list_js_1 = require_list2();
    Object.defineProperty(exports2, "ListBuilder", { enumerable: true, get: function() {
      return list_js_1.ListBuilder;
    } });
    var fixedsizelist_js_1 = require_fixedsizelist();
    Object.defineProperty(exports2, "FixedSizeListBuilder", { enumerable: true, get: function() {
      return fixedsizelist_js_1.FixedSizeListBuilder;
    } });
    var map_js_2 = require_map3();
    Object.defineProperty(exports2, "MapBuilder", { enumerable: true, get: function() {
      return map_js_2.MapBuilder;
    } });
    var struct_js_2 = require_struct3();
    Object.defineProperty(exports2, "StructBuilder", { enumerable: true, get: function() {
      return struct_js_2.StructBuilder;
    } });
    var union_js_1 = require_union2();
    Object.defineProperty(exports2, "UnionBuilder", { enumerable: true, get: function() {
      return union_js_1.UnionBuilder;
    } });
    Object.defineProperty(exports2, "SparseUnionBuilder", { enumerable: true, get: function() {
      return union_js_1.SparseUnionBuilder;
    } });
    Object.defineProperty(exports2, "DenseUnionBuilder", { enumerable: true, get: function() {
      return union_js_1.DenseUnionBuilder;
    } });
    var stream_js_1 = require_stream();
    Object.defineProperty(exports2, "ByteStream", { enumerable: true, get: function() {
      return stream_js_1.ByteStream;
    } });
    Object.defineProperty(exports2, "AsyncByteStream", { enumerable: true, get: function() {
      return stream_js_1.AsyncByteStream;
    } });
    Object.defineProperty(exports2, "AsyncByteQueue", { enumerable: true, get: function() {
      return stream_js_1.AsyncByteQueue;
    } });
    var reader_js_1 = require_reader();
    Object.defineProperty(exports2, "RecordBatchReader", { enumerable: true, get: function() {
      return reader_js_1.RecordBatchReader;
    } });
    Object.defineProperty(exports2, "RecordBatchFileReader", { enumerable: true, get: function() {
      return reader_js_1.RecordBatchFileReader;
    } });
    Object.defineProperty(exports2, "RecordBatchStreamReader", { enumerable: true, get: function() {
      return reader_js_1.RecordBatchStreamReader;
    } });
    Object.defineProperty(exports2, "AsyncRecordBatchFileReader", { enumerable: true, get: function() {
      return reader_js_1.AsyncRecordBatchFileReader;
    } });
    Object.defineProperty(exports2, "AsyncRecordBatchStreamReader", { enumerable: true, get: function() {
      return reader_js_1.AsyncRecordBatchStreamReader;
    } });
    var writer_js_1 = require_writer();
    Object.defineProperty(exports2, "RecordBatchWriter", { enumerable: true, get: function() {
      return writer_js_1.RecordBatchWriter;
    } });
    Object.defineProperty(exports2, "RecordBatchFileWriter", { enumerable: true, get: function() {
      return writer_js_1.RecordBatchFileWriter;
    } });
    Object.defineProperty(exports2, "RecordBatchStreamWriter", { enumerable: true, get: function() {
      return writer_js_1.RecordBatchStreamWriter;
    } });
    Object.defineProperty(exports2, "RecordBatchJSONWriter", { enumerable: true, get: function() {
      return writer_js_1.RecordBatchJSONWriter;
    } });
    var serialization_js_1 = require_serialization();
    Object.defineProperty(exports2, "tableToIPC", { enumerable: true, get: function() {
      return serialization_js_1.tableToIPC;
    } });
    Object.defineProperty(exports2, "tableFromIPC", { enumerable: true, get: function() {
      return serialization_js_1.tableFromIPC;
    } });
    var message_js_1 = require_message3();
    Object.defineProperty(exports2, "MessageReader", { enumerable: true, get: function() {
      return message_js_1.MessageReader;
    } });
    Object.defineProperty(exports2, "AsyncMessageReader", { enumerable: true, get: function() {
      return message_js_1.AsyncMessageReader;
    } });
    Object.defineProperty(exports2, "JSONMessageReader", { enumerable: true, get: function() {
      return message_js_1.JSONMessageReader;
    } });
    var message_js_2 = require_message2();
    Object.defineProperty(exports2, "Message", { enumerable: true, get: function() {
      return message_js_2.Message;
    } });
    var recordbatch_js_1 = require_recordbatch2();
    Object.defineProperty(exports2, "RecordBatch", { enumerable: true, get: function() {
      return recordbatch_js_1.RecordBatch;
    } });
    var util_bn_ = require_bn();
    var util_int_ = require_int2();
    var util_bit_ = require_bit();
    var util_math_ = require_math();
    var util_buffer_ = require_buffer();
    var util_vector_ = require_vector();
    var util_pretty_ = require_pretty();
    var typecomparator_js_1 = require_typecomparator();
    exports2.util = Object.assign(Object.assign(Object.assign(Object.assign(Object.assign(Object.assign(Object.assign(Object.assign({}, util_bn_), util_int_), util_bit_), util_math_), util_buffer_), util_vector_), util_pretty_), {
      compareSchemas: typecomparator_js_1.compareSchemas,
      compareFields: typecomparator_js_1.compareFields,
      compareTypes: typecomparator_js_1.compareTypes
    });
  }
});

// node_modules/apache-arrow/Arrow.dom.js
var require_Arrow_dom = __commonJS({
  "node_modules/apache-arrow/Arrow.dom.js"(exports2) {
    "use strict";
    Object.defineProperty(exports2, "__esModule", { value: true });
    exports2.Union = exports2.StructRow = exports2.Struct = exports2.List = exports2.Decimal = exports2.TimeNanosecond = exports2.TimeMicrosecond = exports2.TimeMillisecond = exports2.TimeSecond = exports2.Time = exports2.TimestampNanosecond = exports2.TimestampMicrosecond = exports2.TimestampMillisecond = exports2.TimestampSecond = exports2.Timestamp = exports2.DateMillisecond = exports2.DateDay = exports2.Date_ = exports2.FixedSizeBinary = exports2.LargeBinary = exports2.Binary = exports2.LargeUtf8 = exports2.Utf8 = exports2.Float64 = exports2.Float32 = exports2.Float16 = exports2.Float = exports2.Uint64 = exports2.Uint32 = exports2.Uint16 = exports2.Uint8 = exports2.Int64 = exports2.Int32 = exports2.Int16 = exports2.Int8 = exports2.Int = exports2.Bool = exports2.Null = exports2.DataType = exports2.makeData = exports2.Data = exports2.BufferType = exports2.UnionMode = exports2.Type = exports2.TimeUnit = exports2.Precision = exports2.MetadataVersion = exports2.MessageHeader = exports2.IntervalUnit = exports2.DateUnit = void 0;
    exports2.LargeBinaryBuilder = exports2.BinaryBuilder = exports2.builderThroughAsyncIterable = exports2.builderThroughIterable = exports2.makeBuilder = exports2.Builder = exports2.util = exports2.RecordBatch = exports2.Message = exports2.JSONMessageReader = exports2.AsyncMessageReader = exports2.MessageReader = exports2.tableToIPC = exports2.tableFromIPC = exports2.RecordBatchJSONWriter = exports2.RecordBatchStreamWriter = exports2.RecordBatchFileWriter = exports2.RecordBatchWriter = exports2.AsyncRecordBatchStreamReader = exports2.AsyncRecordBatchFileReader = exports2.RecordBatchStreamReader = exports2.RecordBatchFileReader = exports2.RecordBatchReader = exports2.AsyncByteQueue = exports2.AsyncByteStream = exports2.ByteStream = exports2.tableFromJSON = exports2.vectorFromArray = exports2.makeVector = exports2.Vector = exports2.Visitor = exports2.Field = exports2.Schema = exports2.tableFromArrays = exports2.makeTable = exports2.Table = exports2.MapRow = exports2.Map_ = exports2.FixedSizeList = exports2.DurationNanosecond = exports2.DurationMicrosecond = exports2.DurationMillisecond = exports2.DurationSecond = exports2.Duration = exports2.IntervalYearMonth = exports2.IntervalDayTime = exports2.Interval = exports2.Dictionary = exports2.SparseUnion = exports2.DenseUnion = void 0;
    exports2.LargeUtf8Builder = exports2.Utf8Builder = exports2.SparseUnionBuilder = exports2.DenseUnionBuilder = exports2.UnionBuilder = exports2.TimeNanosecondBuilder = exports2.TimeMicrosecondBuilder = exports2.TimeMillisecondBuilder = exports2.TimeSecondBuilder = exports2.TimeBuilder = exports2.TimestampNanosecondBuilder = exports2.TimestampMicrosecondBuilder = exports2.TimestampMillisecondBuilder = exports2.TimestampSecondBuilder = exports2.TimestampBuilder = exports2.StructBuilder = exports2.NullBuilder = exports2.MapBuilder = exports2.ListBuilder = exports2.Uint64Builder = exports2.Uint32Builder = exports2.Uint16Builder = exports2.Uint8Builder = exports2.Int64Builder = exports2.Int32Builder = exports2.Int16Builder = exports2.Int8Builder = exports2.IntBuilder = exports2.DurationNanosecondBuilder = exports2.DurationMicrosecondBuilder = exports2.DurationMillisecondBuilder = exports2.DurationSecondBuilder = exports2.DurationBuilder = exports2.IntervalYearMonthBuilder = exports2.IntervalDayTimeBuilder = exports2.IntervalBuilder = exports2.Float64Builder = exports2.Float32Builder = exports2.Float16Builder = exports2.FloatBuilder = exports2.FixedSizeListBuilder = exports2.FixedSizeBinaryBuilder = exports2.DictionaryBuilder = exports2.DecimalBuilder = exports2.DateMillisecondBuilder = exports2.DateDayBuilder = exports2.DateBuilder = exports2.BoolBuilder = void 0;
    var adapters_js_1 = require_adapters();
    var builder_js_1 = require_builder();
    var reader_js_1 = require_reader();
    var writer_js_1 = require_writer();
    var iterable_js_1 = require_iterable2();
    var builder_js_2 = require_builder3();
    var reader_js_2 = require_reader3();
    var writer_js_2 = require_writer3();
    adapters_js_1.default.toDOMStream = iterable_js_1.toDOMStream;
    builder_js_1.Builder["throughDOM"] = builder_js_2.builderThroughDOMStream;
    reader_js_1.RecordBatchReader["throughDOM"] = reader_js_2.recordBatchReaderThroughDOMStream;
    reader_js_1.RecordBatchFileReader["throughDOM"] = reader_js_2.recordBatchReaderThroughDOMStream;
    reader_js_1.RecordBatchStreamReader["throughDOM"] = reader_js_2.recordBatchReaderThroughDOMStream;
    writer_js_1.RecordBatchWriter["throughDOM"] = writer_js_2.recordBatchWriterThroughDOMStream;
    writer_js_1.RecordBatchFileWriter["throughDOM"] = writer_js_2.recordBatchWriterThroughDOMStream;
    writer_js_1.RecordBatchStreamWriter["throughDOM"] = writer_js_2.recordBatchWriterThroughDOMStream;
    var Arrow_js_1 = require_Arrow();
    Object.defineProperty(exports2, "DateUnit", { enumerable: true, get: function() {
      return Arrow_js_1.DateUnit;
    } });
    Object.defineProperty(exports2, "IntervalUnit", { enumerable: true, get: function() {
      return Arrow_js_1.IntervalUnit;
    } });
    Object.defineProperty(exports2, "MessageHeader", { enumerable: true, get: function() {
      return Arrow_js_1.MessageHeader;
    } });
    Object.defineProperty(exports2, "MetadataVersion", { enumerable: true, get: function() {
      return Arrow_js_1.MetadataVersion;
    } });
    Object.defineProperty(exports2, "Precision", { enumerable: true, get: function() {
      return Arrow_js_1.Precision;
    } });
    Object.defineProperty(exports2, "TimeUnit", { enumerable: true, get: function() {
      return Arrow_js_1.TimeUnit;
    } });
    Object.defineProperty(exports2, "Type", { enumerable: true, get: function() {
      return Arrow_js_1.Type;
    } });
    Object.defineProperty(exports2, "UnionMode", { enumerable: true, get: function() {
      return Arrow_js_1.UnionMode;
    } });
    Object.defineProperty(exports2, "BufferType", { enumerable: true, get: function() {
      return Arrow_js_1.BufferType;
    } });
    Object.defineProperty(exports2, "Data", { enumerable: true, get: function() {
      return Arrow_js_1.Data;
    } });
    Object.defineProperty(exports2, "makeData", { enumerable: true, get: function() {
      return Arrow_js_1.makeData;
    } });
    Object.defineProperty(exports2, "DataType", { enumerable: true, get: function() {
      return Arrow_js_1.DataType;
    } });
    Object.defineProperty(exports2, "Null", { enumerable: true, get: function() {
      return Arrow_js_1.Null;
    } });
    Object.defineProperty(exports2, "Bool", { enumerable: true, get: function() {
      return Arrow_js_1.Bool;
    } });
    Object.defineProperty(exports2, "Int", { enumerable: true, get: function() {
      return Arrow_js_1.Int;
    } });
    Object.defineProperty(exports2, "Int8", { enumerable: true, get: function() {
      return Arrow_js_1.Int8;
    } });
    Object.defineProperty(exports2, "Int16", { enumerable: true, get: function() {
      return Arrow_js_1.Int16;
    } });
    Object.defineProperty(exports2, "Int32", { enumerable: true, get: function() {
      return Arrow_js_1.Int32;
    } });
    Object.defineProperty(exports2, "Int64", { enumerable: true, get: function() {
      return Arrow_js_1.Int64;
    } });
    Object.defineProperty(exports2, "Uint8", { enumerable: true, get: function() {
      return Arrow_js_1.Uint8;
    } });
    Object.defineProperty(exports2, "Uint16", { enumerable: true, get: function() {
      return Arrow_js_1.Uint16;
    } });
    Object.defineProperty(exports2, "Uint32", { enumerable: true, get: function() {
      return Arrow_js_1.Uint32;
    } });
    Object.defineProperty(exports2, "Uint64", { enumerable: true, get: function() {
      return Arrow_js_1.Uint64;
    } });
    Object.defineProperty(exports2, "Float", { enumerable: true, get: function() {
      return Arrow_js_1.Float;
    } });
    Object.defineProperty(exports2, "Float16", { enumerable: true, get: function() {
      return Arrow_js_1.Float16;
    } });
    Object.defineProperty(exports2, "Float32", { enumerable: true, get: function() {
      return Arrow_js_1.Float32;
    } });
    Object.defineProperty(exports2, "Float64", { enumerable: true, get: function() {
      return Arrow_js_1.Float64;
    } });
    Object.defineProperty(exports2, "Utf8", { enumerable: true, get: function() {
      return Arrow_js_1.Utf8;
    } });
    Object.defineProperty(exports2, "LargeUtf8", { enumerable: true, get: function() {
      return Arrow_js_1.LargeUtf8;
    } });
    Object.defineProperty(exports2, "Binary", { enumerable: true, get: function() {
      return Arrow_js_1.Binary;
    } });
    Object.defineProperty(exports2, "LargeBinary", { enumerable: true, get: function() {
      return Arrow_js_1.LargeBinary;
    } });
    Object.defineProperty(exports2, "FixedSizeBinary", { enumerable: true, get: function() {
      return Arrow_js_1.FixedSizeBinary;
    } });
    Object.defineProperty(exports2, "Date_", { enumerable: true, get: function() {
      return Arrow_js_1.Date_;
    } });
    Object.defineProperty(exports2, "DateDay", { enumerable: true, get: function() {
      return Arrow_js_1.DateDay;
    } });
    Object.defineProperty(exports2, "DateMillisecond", { enumerable: true, get: function() {
      return Arrow_js_1.DateMillisecond;
    } });
    Object.defineProperty(exports2, "Timestamp", { enumerable: true, get: function() {
      return Arrow_js_1.Timestamp;
    } });
    Object.defineProperty(exports2, "TimestampSecond", { enumerable: true, get: function() {
      return Arrow_js_1.TimestampSecond;
    } });
    Object.defineProperty(exports2, "TimestampMillisecond", { enumerable: true, get: function() {
      return Arrow_js_1.TimestampMillisecond;
    } });
    Object.defineProperty(exports2, "TimestampMicrosecond", { enumerable: true, get: function() {
      return Arrow_js_1.TimestampMicrosecond;
    } });
    Object.defineProperty(exports2, "TimestampNanosecond", { enumerable: true, get: function() {
      return Arrow_js_1.TimestampNanosecond;
    } });
    Object.defineProperty(exports2, "Time", { enumerable: true, get: function() {
      return Arrow_js_1.Time;
    } });
    Object.defineProperty(exports2, "TimeSecond", { enumerable: true, get: function() {
      return Arrow_js_1.TimeSecond;
    } });
    Object.defineProperty(exports2, "TimeMillisecond", { enumerable: true, get: function() {
      return Arrow_js_1.TimeMillisecond;
    } });
    Object.defineProperty(exports2, "TimeMicrosecond", { enumerable: true, get: function() {
      return Arrow_js_1.TimeMicrosecond;
    } });
    Object.defineProperty(exports2, "TimeNanosecond", { enumerable: true, get: function() {
      return Arrow_js_1.TimeNanosecond;
    } });
    Object.defineProperty(exports2, "Decimal", { enumerable: true, get: function() {
      return Arrow_js_1.Decimal;
    } });
    Object.defineProperty(exports2, "List", { enumerable: true, get: function() {
      return Arrow_js_1.List;
    } });
    Object.defineProperty(exports2, "Struct", { enumerable: true, get: function() {
      return Arrow_js_1.Struct;
    } });
    Object.defineProperty(exports2, "StructRow", { enumerable: true, get: function() {
      return Arrow_js_1.StructRow;
    } });
    Object.defineProperty(exports2, "Union", { enumerable: true, get: function() {
      return Arrow_js_1.Union;
    } });
    Object.defineProperty(exports2, "DenseUnion", { enumerable: true, get: function() {
      return Arrow_js_1.DenseUnion;
    } });
    Object.defineProperty(exports2, "SparseUnion", { enumerable: true, get: function() {
      return Arrow_js_1.SparseUnion;
    } });
    Object.defineProperty(exports2, "Dictionary", { enumerable: true, get: function() {
      return Arrow_js_1.Dictionary;
    } });
    Object.defineProperty(exports2, "Interval", { enumerable: true, get: function() {
      return Arrow_js_1.Interval;
    } });
    Object.defineProperty(exports2, "IntervalDayTime", { enumerable: true, get: function() {
      return Arrow_js_1.IntervalDayTime;
    } });
    Object.defineProperty(exports2, "IntervalYearMonth", { enumerable: true, get: function() {
      return Arrow_js_1.IntervalYearMonth;
    } });
    Object.defineProperty(exports2, "Duration", { enumerable: true, get: function() {
      return Arrow_js_1.Duration;
    } });
    Object.defineProperty(exports2, "DurationSecond", { enumerable: true, get: function() {
      return Arrow_js_1.DurationSecond;
    } });
    Object.defineProperty(exports2, "DurationMillisecond", { enumerable: true, get: function() {
      return Arrow_js_1.DurationMillisecond;
    } });
    Object.defineProperty(exports2, "DurationMicrosecond", { enumerable: true, get: function() {
      return Arrow_js_1.DurationMicrosecond;
    } });
    Object.defineProperty(exports2, "DurationNanosecond", { enumerable: true, get: function() {
      return Arrow_js_1.DurationNanosecond;
    } });
    Object.defineProperty(exports2, "FixedSizeList", { enumerable: true, get: function() {
      return Arrow_js_1.FixedSizeList;
    } });
    Object.defineProperty(exports2, "Map_", { enumerable: true, get: function() {
      return Arrow_js_1.Map_;
    } });
    Object.defineProperty(exports2, "MapRow", { enumerable: true, get: function() {
      return Arrow_js_1.MapRow;
    } });
    Object.defineProperty(exports2, "Table", { enumerable: true, get: function() {
      return Arrow_js_1.Table;
    } });
    Object.defineProperty(exports2, "makeTable", { enumerable: true, get: function() {
      return Arrow_js_1.makeTable;
    } });
    Object.defineProperty(exports2, "tableFromArrays", { enumerable: true, get: function() {
      return Arrow_js_1.tableFromArrays;
    } });
    Object.defineProperty(exports2, "Schema", { enumerable: true, get: function() {
      return Arrow_js_1.Schema;
    } });
    Object.defineProperty(exports2, "Field", { enumerable: true, get: function() {
      return Arrow_js_1.Field;
    } });
    Object.defineProperty(exports2, "Visitor", { enumerable: true, get: function() {
      return Arrow_js_1.Visitor;
    } });
    Object.defineProperty(exports2, "Vector", { enumerable: true, get: function() {
      return Arrow_js_1.Vector;
    } });
    Object.defineProperty(exports2, "makeVector", { enumerable: true, get: function() {
      return Arrow_js_1.makeVector;
    } });
    Object.defineProperty(exports2, "vectorFromArray", { enumerable: true, get: function() {
      return Arrow_js_1.vectorFromArray;
    } });
    Object.defineProperty(exports2, "tableFromJSON", { enumerable: true, get: function() {
      return Arrow_js_1.tableFromJSON;
    } });
    Object.defineProperty(exports2, "ByteStream", { enumerable: true, get: function() {
      return Arrow_js_1.ByteStream;
    } });
    Object.defineProperty(exports2, "AsyncByteStream", { enumerable: true, get: function() {
      return Arrow_js_1.AsyncByteStream;
    } });
    Object.defineProperty(exports2, "AsyncByteQueue", { enumerable: true, get: function() {
      return Arrow_js_1.AsyncByteQueue;
    } });
    Object.defineProperty(exports2, "RecordBatchReader", { enumerable: true, get: function() {
      return Arrow_js_1.RecordBatchReader;
    } });
    Object.defineProperty(exports2, "RecordBatchFileReader", { enumerable: true, get: function() {
      return Arrow_js_1.RecordBatchFileReader;
    } });
    Object.defineProperty(exports2, "RecordBatchStreamReader", { enumerable: true, get: function() {
      return Arrow_js_1.RecordBatchStreamReader;
    } });
    Object.defineProperty(exports2, "AsyncRecordBatchFileReader", { enumerable: true, get: function() {
      return Arrow_js_1.AsyncRecordBatchFileReader;
    } });
    Object.defineProperty(exports2, "AsyncRecordBatchStreamReader", { enumerable: true, get: function() {
      return Arrow_js_1.AsyncRecordBatchStreamReader;
    } });
    Object.defineProperty(exports2, "RecordBatchWriter", { enumerable: true, get: function() {
      return Arrow_js_1.RecordBatchWriter;
    } });
    Object.defineProperty(exports2, "RecordBatchFileWriter", { enumerable: true, get: function() {
      return Arrow_js_1.RecordBatchFileWriter;
    } });
    Object.defineProperty(exports2, "RecordBatchStreamWriter", { enumerable: true, get: function() {
      return Arrow_js_1.RecordBatchStreamWriter;
    } });
    Object.defineProperty(exports2, "RecordBatchJSONWriter", { enumerable: true, get: function() {
      return Arrow_js_1.RecordBatchJSONWriter;
    } });
    Object.defineProperty(exports2, "tableFromIPC", { enumerable: true, get: function() {
      return Arrow_js_1.tableFromIPC;
    } });
    Object.defineProperty(exports2, "tableToIPC", { enumerable: true, get: function() {
      return Arrow_js_1.tableToIPC;
    } });
    Object.defineProperty(exports2, "MessageReader", { enumerable: true, get: function() {
      return Arrow_js_1.MessageReader;
    } });
    Object.defineProperty(exports2, "AsyncMessageReader", { enumerable: true, get: function() {
      return Arrow_js_1.AsyncMessageReader;
    } });
    Object.defineProperty(exports2, "JSONMessageReader", { enumerable: true, get: function() {
      return Arrow_js_1.JSONMessageReader;
    } });
    Object.defineProperty(exports2, "Message", { enumerable: true, get: function() {
      return Arrow_js_1.Message;
    } });
    Object.defineProperty(exports2, "RecordBatch", { enumerable: true, get: function() {
      return Arrow_js_1.RecordBatch;
    } });
    Object.defineProperty(exports2, "util", { enumerable: true, get: function() {
      return Arrow_js_1.util;
    } });
    Object.defineProperty(exports2, "Builder", { enumerable: true, get: function() {
      return Arrow_js_1.Builder;
    } });
    Object.defineProperty(exports2, "makeBuilder", { enumerable: true, get: function() {
      return Arrow_js_1.makeBuilder;
    } });
    Object.defineProperty(exports2, "builderThroughIterable", { enumerable: true, get: function() {
      return Arrow_js_1.builderThroughIterable;
    } });
    Object.defineProperty(exports2, "builderThroughAsyncIterable", { enumerable: true, get: function() {
      return Arrow_js_1.builderThroughAsyncIterable;
    } });
    var Arrow_js_2 = require_Arrow();
    Object.defineProperty(exports2, "BinaryBuilder", { enumerable: true, get: function() {
      return Arrow_js_2.BinaryBuilder;
    } });
    Object.defineProperty(exports2, "LargeBinaryBuilder", { enumerable: true, get: function() {
      return Arrow_js_2.LargeBinaryBuilder;
    } });
    Object.defineProperty(exports2, "BoolBuilder", { enumerable: true, get: function() {
      return Arrow_js_2.BoolBuilder;
    } });
    Object.defineProperty(exports2, "DateBuilder", { enumerable: true, get: function() {
      return Arrow_js_2.DateBuilder;
    } });
    Object.defineProperty(exports2, "DateDayBuilder", { enumerable: true, get: function() {
      return Arrow_js_2.DateDayBuilder;
    } });
    Object.defineProperty(exports2, "DateMillisecondBuilder", { enumerable: true, get: function() {
      return Arrow_js_2.DateMillisecondBuilder;
    } });
    Object.defineProperty(exports2, "DecimalBuilder", { enumerable: true, get: function() {
      return Arrow_js_2.DecimalBuilder;
    } });
    Object.defineProperty(exports2, "DictionaryBuilder", { enumerable: true, get: function() {
      return Arrow_js_2.DictionaryBuilder;
    } });
    Object.defineProperty(exports2, "FixedSizeBinaryBuilder", { enumerable: true, get: function() {
      return Arrow_js_2.FixedSizeBinaryBuilder;
    } });
    Object.defineProperty(exports2, "FixedSizeListBuilder", { enumerable: true, get: function() {
      return Arrow_js_2.FixedSizeListBuilder;
    } });
    Object.defineProperty(exports2, "FloatBuilder", { enumerable: true, get: function() {
      return Arrow_js_2.FloatBuilder;
    } });
    Object.defineProperty(exports2, "Float16Builder", { enumerable: true, get: function() {
      return Arrow_js_2.Float16Builder;
    } });
    Object.defineProperty(exports2, "Float32Builder", { enumerable: true, get: function() {
      return Arrow_js_2.Float32Builder;
    } });
    Object.defineProperty(exports2, "Float64Builder", { enumerable: true, get: function() {
      return Arrow_js_2.Float64Builder;
    } });
    Object.defineProperty(exports2, "IntervalBuilder", { enumerable: true, get: function() {
      return Arrow_js_2.IntervalBuilder;
    } });
    Object.defineProperty(exports2, "IntervalDayTimeBuilder", { enumerable: true, get: function() {
      return Arrow_js_2.IntervalDayTimeBuilder;
    } });
    Object.defineProperty(exports2, "IntervalYearMonthBuilder", { enumerable: true, get: function() {
      return Arrow_js_2.IntervalYearMonthBuilder;
    } });
    Object.defineProperty(exports2, "DurationBuilder", { enumerable: true, get: function() {
      return Arrow_js_2.DurationBuilder;
    } });
    Object.defineProperty(exports2, "DurationSecondBuilder", { enumerable: true, get: function() {
      return Arrow_js_2.DurationSecondBuilder;
    } });
    Object.defineProperty(exports2, "DurationMillisecondBuilder", { enumerable: true, get: function() {
      return Arrow_js_2.DurationMillisecondBuilder;
    } });
    Object.defineProperty(exports2, "DurationMicrosecondBuilder", { enumerable: true, get: function() {
      return Arrow_js_2.DurationMicrosecondBuilder;
    } });
    Object.defineProperty(exports2, "DurationNanosecondBuilder", { enumerable: true, get: function() {
      return Arrow_js_2.DurationNanosecondBuilder;
    } });
    Object.defineProperty(exports2, "IntBuilder", { enumerable: true, get: function() {
      return Arrow_js_2.IntBuilder;
    } });
    Object.defineProperty(exports2, "Int8Builder", { enumerable: true, get: function() {
      return Arrow_js_2.Int8Builder;
    } });
    Object.defineProperty(exports2, "Int16Builder", { enumerable: true, get: function() {
      return Arrow_js_2.Int16Builder;
    } });
    Object.defineProperty(exports2, "Int32Builder", { enumerable: true, get: function() {
      return Arrow_js_2.Int32Builder;
    } });
    Object.defineProperty(exports2, "Int64Builder", { enumerable: true, get: function() {
      return Arrow_js_2.Int64Builder;
    } });
    Object.defineProperty(exports2, "Uint8Builder", { enumerable: true, get: function() {
      return Arrow_js_2.Uint8Builder;
    } });
    Object.defineProperty(exports2, "Uint16Builder", { enumerable: true, get: function() {
      return Arrow_js_2.Uint16Builder;
    } });
    Object.defineProperty(exports2, "Uint32Builder", { enumerable: true, get: function() {
      return Arrow_js_2.Uint32Builder;
    } });
    Object.defineProperty(exports2, "Uint64Builder", { enumerable: true, get: function() {
      return Arrow_js_2.Uint64Builder;
    } });
    Object.defineProperty(exports2, "ListBuilder", { enumerable: true, get: function() {
      return Arrow_js_2.ListBuilder;
    } });
    Object.defineProperty(exports2, "MapBuilder", { enumerable: true, get: function() {
      return Arrow_js_2.MapBuilder;
    } });
    Object.defineProperty(exports2, "NullBuilder", { enumerable: true, get: function() {
      return Arrow_js_2.NullBuilder;
    } });
    Object.defineProperty(exports2, "StructBuilder", { enumerable: true, get: function() {
      return Arrow_js_2.StructBuilder;
    } });
    Object.defineProperty(exports2, "TimestampBuilder", { enumerable: true, get: function() {
      return Arrow_js_2.TimestampBuilder;
    } });
    Object.defineProperty(exports2, "TimestampSecondBuilder", { enumerable: true, get: function() {
      return Arrow_js_2.TimestampSecondBuilder;
    } });
    Object.defineProperty(exports2, "TimestampMillisecondBuilder", { enumerable: true, get: function() {
      return Arrow_js_2.TimestampMillisecondBuilder;
    } });
    Object.defineProperty(exports2, "TimestampMicrosecondBuilder", { enumerable: true, get: function() {
      return Arrow_js_2.TimestampMicrosecondBuilder;
    } });
    Object.defineProperty(exports2, "TimestampNanosecondBuilder", { enumerable: true, get: function() {
      return Arrow_js_2.TimestampNanosecondBuilder;
    } });
    Object.defineProperty(exports2, "TimeBuilder", { enumerable: true, get: function() {
      return Arrow_js_2.TimeBuilder;
    } });
    Object.defineProperty(exports2, "TimeSecondBuilder", { enumerable: true, get: function() {
      return Arrow_js_2.TimeSecondBuilder;
    } });
    Object.defineProperty(exports2, "TimeMillisecondBuilder", { enumerable: true, get: function() {
      return Arrow_js_2.TimeMillisecondBuilder;
    } });
    Object.defineProperty(exports2, "TimeMicrosecondBuilder", { enumerable: true, get: function() {
      return Arrow_js_2.TimeMicrosecondBuilder;
    } });
    Object.defineProperty(exports2, "TimeNanosecondBuilder", { enumerable: true, get: function() {
      return Arrow_js_2.TimeNanosecondBuilder;
    } });
    Object.defineProperty(exports2, "UnionBuilder", { enumerable: true, get: function() {
      return Arrow_js_2.UnionBuilder;
    } });
    Object.defineProperty(exports2, "DenseUnionBuilder", { enumerable: true, get: function() {
      return Arrow_js_2.DenseUnionBuilder;
    } });
    Object.defineProperty(exports2, "SparseUnionBuilder", { enumerable: true, get: function() {
      return Arrow_js_2.SparseUnionBuilder;
    } });
    Object.defineProperty(exports2, "Utf8Builder", { enumerable: true, get: function() {
      return Arrow_js_2.Utf8Builder;
    } });
    Object.defineProperty(exports2, "LargeUtf8Builder", { enumerable: true, get: function() {
      return Arrow_js_2.LargeUtf8Builder;
    } });
  }
});

// node_modules/apache-arrow/Arrow.node.js
var require_Arrow_node = __commonJS({
  "node_modules/apache-arrow/Arrow.node.js"(exports2) {
    "use strict";
    Object.defineProperty(exports2, "__esModule", { value: true });
    var tslib_1 = (init_tslib_es6(), __toCommonJS(tslib_es6_exports));
    var adapters_js_1 = require_adapters();
    var builder_js_1 = require_builder();
    var reader_js_1 = require_reader();
    var writer_js_1 = require_writer();
    var iterable_js_1 = require_iterable();
    var builder_js_2 = require_builder2();
    var reader_js_2 = require_reader2();
    var writer_js_2 = require_writer2();
    adapters_js_1.default.toNodeStream = iterable_js_1.toNodeStream;
    builder_js_1.Builder["throughNode"] = builder_js_2.builderThroughNodeStream;
    reader_js_1.RecordBatchReader["throughNode"] = reader_js_2.recordBatchReaderThroughNodeStream;
    writer_js_1.RecordBatchWriter["throughNode"] = writer_js_2.recordBatchWriterThroughNodeStream;
    tslib_1.__exportStar(require_Arrow_dom(), exports2);
  }
});

// node_modules/@duckdb/duckdb-wasm/dist/duckdb-node.cjs
var require_duckdb_node = __commonJS({
  "node_modules/@duckdb/duckdb-wasm/dist/duckdb-node.cjs"(exports2, module2) {
    "use strict";
    var pe = Object.create;
    var g = Object.defineProperty;
    var _e = Object.getOwnPropertyDescriptor;
    var me = Object.getOwnPropertyNames;
    var Re = Object.getPrototypeOf;
    var Te = Object.prototype.hasOwnProperty;
    var be = (s, e) => () => (e || s((e = { exports: {} }).exports, e), e.exports);
    var Ie = (s, e) => {
      for (var r in e) g(s, r, { get: e[r], enumerable: true });
    };
    var j = (s, e, r, t) => {
      if (e && typeof e == "object" || typeof e == "function") for (let n of me(e)) !Te.call(s, n) && n !== r && g(s, n, { get: () => e[n], enumerable: !(t = _e(e, n)) || t.enumerable });
      return s;
    };
    var N = (s, e, r) => (r = s != null ? pe(Re(s)) : {}, j(e || !s || !s.__esModule ? g(r, "default", { value: s, enumerable: true }) : r, s));
    var ge = (s) => j(g({}, "__esModule", { value: true }), s);
    var de = be((Ir, le) => {
      var q = require("url"), Ge = require("vm"), R = require("worker_threads"), H = /* @__PURE__ */ Symbol.for("worker"), I = /* @__PURE__ */ Symbol.for("events"), P = class {
        constructor() {
          Object.defineProperty(this, I, { value: /* @__PURE__ */ new Map() });
        }
        dispatchEvent(e) {
          if (e.target = e.currentTarget = this, this["on" + e.type]) try {
            this["on" + e.type](e);
          } catch (t) {
            console.error(t);
          }
          let r = this[I].get(e.type);
          r != null && r.forEach((t) => {
            try {
              t.call(this, e);
            } catch (n) {
              console.error(n);
            }
          });
        }
        addEventListener(e, r) {
          let t = this[I].get(e);
          t || this[I].set(e, t = []), t.push(r);
        }
        removeEventListener(e, r) {
          let t = this[I].get(e);
          if (t) {
            let n = t.indexOf(r);
            n !== -1 && t.splice(n, 1);
          }
        }
      };
      function Y(s, e) {
        this.type = s, this.timeStamp = Date.now(), this.target = this.currentTarget = this.data = null;
      }
      le.exports = R.isMainThread ? Qe() : He();
      var xe = q.pathToFileURL(process.cwd() + "/");
      function Qe() {
        class s extends P {
          constructor(r, t) {
            super();
            let { name: n, type: a } = t || {};
            r += "";
            let o;
            /^data:/.test(r) ? o = r : o = q.fileURLToPath(new q.URL(r, xe));
            let E = new R.Worker(__filename, { workerData: { mod: o, name: n, type: a } });
            Object.defineProperty(this, H, { value: E }), E.on("message", (m) => {
              let u = new Y("message");
              u.data = m, this.dispatchEvent(u);
            }), E.on("error", (m) => {
              m.type = "error", this.dispatchEvent(m);
            }), E.on("exit", () => {
              this.dispatchEvent(new Y("close"));
            });
          }
          postMessage(r, t) {
            this[H].postMessage(r, t);
          }
          terminate() {
            this[H].terminate();
          }
        }
        return s.prototype.onmessage = s.prototype.onerror = s.prototype.onclose = null, s;
      }
      function He() {
        let { mod: s, name: e, type: r } = R.workerData, t = global.self = global, n = [];
        function a() {
          let u = n;
          n = null, u.forEach((_) => {
            t.dispatchEvent(_);
          });
        }
        R.parentPort.on("message", (u) => {
          let _ = new Y("message");
          _.data = u, n == null ? t.dispatchEvent(_) : n.push(_);
        }), R.parentPort.on("error", (u) => {
          u.type = "Error", t.dispatchEvent(u);
        });
        class o extends P {
          postMessage(_, Ee) {
            R.parentPort.postMessage(_, Ee);
          }
          close() {
            process.exit();
          }
        }
        let E = Object.getPrototypeOf(global);
        delete E.constructor, Object.defineProperties(o.prototype, E), E = Object.setPrototypeOf(global, new o()), ["postMessage", "addEventListener", "removeEventListener", "dispatchEvent"].forEach((u) => {
          E[u] = E[u].bind(global);
        }), global.name = e;
        let m = /^data:/.test(s);
        if (r === "module") import(s).catch((u) => {
          if (m && u.message === "Not supported") return console.warn("Worker(): Importing data: URLs requires Node 12.10+. Falling back to classic worker."), ce(s, e);
          console.error(u);
        }).then(a);
        else {
          try {
            /^data:/.test(s) ? ce(s, e) : require(s);
          } catch (u) {
            console.error(u);
          }
          Promise.resolve().then(a);
        }
      }
      function ce(s, e) {
        let { data: r } = qe(s);
        return Ge.runInThisContext(r, { filename: "worker.<" + (e || "data:") + ">" });
      }
      function qe(s) {
        let [e, r, t, n] = s.match(/^data: *([^;,]*)(?: *; *([^,]*))? *,(.*)$/) || [];
        if (!e) throw Error("Invalid Data URL.");
        if (t) switch (t.toLowerCase()) {
          case "base64":
            n = Buffer.from(n, "base64").toString();
            break;
          default:
            throw Error('Unknown Data URL encoding "' + t + '"');
        }
        return { type: r, data: n };
      }
    });
    var je = {};
    Ie(je, { AsyncDuckDB: () => L, AsyncDuckDBConnection: () => T, AsyncDuckDBDispatcher: () => w, AsyncPreparedStatement: () => y, AsyncResultStreamIterator: () => b, ConsoleLogger: () => A, DuckDBAccessMode: () => K, DuckDBDataProtocol: () => F, IsArrowBuffer: () => Pe, IsDuckDBWasmRetry: () => Ne, LogEvent: () => X, LogLevel: () => z, LogOrigin: () => $, LogTopic: () => J, PACKAGE_NAME: () => U, PACKAGE_VERSION: () => C, PACKAGE_VERSION_MAJOR: () => we, PACKAGE_VERSION_MINOR: () => Ue, PACKAGE_VERSION_PATCH: () => Ce, StatusCode: () => Z, TokenType: () => V, VoidLogger: () => f, WorkerRequestType: () => D, WorkerResponseType: () => O, WorkerTask: () => i, createWorker: () => Ye, getJsDelivrBundles: () => Me, getLogEventLabel: () => ke, getLogLevelLabel: () => ye, getLogOriginLabel: () => he, getLogTopicLabel: () => Se, getPlatformFeatures: () => ie, isFirefox: () => We, isNode: () => Q, isSafari: () => ve, selectBundle: () => Be });
    module2.exports = ge(je);
    var K = ((n) => (n[n.UNDEFINED = 0] = "UNDEFINED", n[n.AUTOMATIC = 1] = "AUTOMATIC", n[n.READ_ONLY = 2] = "READ_ONLY", n[n.READ_WRITE = 3] = "READ_WRITE", n))(K || {});
    var V = ((o) => (o[o.IDENTIFIER = 0] = "IDENTIFIER", o[o.NUMERIC_CONSTANT = 1] = "NUMERIC_CONSTANT", o[o.STRING_CONSTANT = 2] = "STRING_CONSTANT", o[o.OPERATOR = 3] = "OPERATOR", o[o.KEYWORD = 4] = "KEYWORD", o[o.COMMENT = 5] = "COMMENT", o))(V || {});
    var z = ((a) => (a[a.NONE = 0] = "NONE", a[a.DEBUG = 1] = "DEBUG", a[a.INFO = 2] = "INFO", a[a.WARNING = 3] = "WARNING", a[a.ERROR = 4] = "ERROR", a))(z || {});
    var J = ((o) => (o[o.NONE = 0] = "NONE", o[o.CONNECT = 1] = "CONNECT", o[o.DISCONNECT = 2] = "DISCONNECT", o[o.OPEN = 3] = "OPEN", o[o.QUERY = 4] = "QUERY", o[o.INSTANTIATE = 5] = "INSTANTIATE", o))(J || {});
    var X = ((o) => (o[o.NONE = 0] = "NONE", o[o.OK = 1] = "OK", o[o.ERROR = 2] = "ERROR", o[o.START = 3] = "START", o[o.RUN = 4] = "RUN", o[o.CAPTURE = 5] = "CAPTURE", o))(X || {});
    var $ = ((a) => (a[a.NONE = 0] = "NONE", a[a.WEB_WORKER = 1] = "WEB_WORKER", a[a.NODE_WORKER = 2] = "NODE_WORKER", a[a.BINDINGS = 3] = "BINDINGS", a[a.ASYNC_DUCKDB = 4] = "ASYNC_DUCKDB", a))($ || {});
    var f = class {
      log(e) {
      }
    };
    var A = class {
      constructor(e = 2) {
        this.level = e;
      }
      log(e) {
        e.level >= this.level && console.log(e);
      }
    };
    function ye(s) {
      switch (s) {
        case 0:
          return "NONE";
        case 1:
          return "DEBUG";
        case 2:
          return "INFO";
        case 3:
          return "WARNING";
        case 4:
          return "ERROR";
        default:
          return "?";
      }
    }
    function ke(s) {
      switch (s) {
        case 0:
          return "NONE";
        case 1:
          return "OK";
        case 2:
          return "ERROR";
        case 3:
          return "START";
        case 4:
          return "RUN";
        case 5:
          return "CAPTURE";
        default:
          return "?";
      }
    }
    function Se(s) {
      switch (s) {
        case 1:
          return "CONNECT";
        case 2:
          return "DISCONNECT";
        case 5:
          return "INSTANTIATE";
        case 3:
          return "OPEN";
        case 4:
          return "QUERY";
        default:
          return "?";
      }
    }
    function he(s) {
      switch (s) {
        case 0:
          return "NONE";
        case 1:
          return "WEB WORKER";
        case 2:
          return "NODE WORKER";
        case 3:
          return "DUCKDB BINDINGS";
        case 4:
          return "DUCKDB";
        default:
          return "?";
      }
    }
    var Z = ((t) => (t[t.SUCCESS = 0] = "SUCCESS", t[t.MAX_ARROW_ERROR = 255] = "MAX_ARROW_ERROR", t[t.DUCKDB_WASM_RETRY = 256] = "DUCKDB_WASM_RETRY", t))(Z || {});
    function Pe(s) {
      return s <= 255;
    }
    function Ne(s) {
      return s === 256;
    }
    var p = N(require_Arrow_node());
    var T = class {
      constructor(e, r) {
        this._bindings = e, this._conn = r;
      }
      get bindings() {
        return this._bindings;
      }
      async close() {
        return this._bindings.disconnect(this._conn);
      }
      useUnsafe(e) {
        return e(this._bindings, this._conn);
      }
      async query(e) {
        this._bindings.logger.log({ timestamp: /* @__PURE__ */ new Date(), level: 2, origin: 4, topic: 4, event: 4, value: e });
        let r = await this._bindings.runQuery(this._conn, e), t = p.RecordBatchReader.from(r);
        return console.assert(t.isSync(), "Reader is not sync"), console.assert(t.isFile(), "Reader is not file"), new p.Table(t);
      }
      async send(e, r = false) {
        this._bindings.logger.log({ timestamp: /* @__PURE__ */ new Date(), level: 2, origin: 4, topic: 4, event: 4, value: e });
        let t = await this._bindings.startPendingQuery(this._conn, e, r);
        for (; t == null; ) {
          if (this._bindings.isDetached()) {
            console.error("cannot send a message since the worker is not set!");
            return;
          }
          t = await this._bindings.pollPendingQuery(this._conn);
        }
        let n = new b(this._bindings, this._conn, t), a = await p.RecordBatchReader.from(n);
        return console.assert(a.isAsync()), console.assert(a.isStream()), a;
      }
      async cancelSent() {
        return await this._bindings.cancelPendingQuery(this._conn);
      }
      async getTableNames(e) {
        return await this._bindings.getTableNames(this._conn, e);
      }
      async prepare(e) {
        let r = await this._bindings.createPrepared(this._conn, e);
        return new y(this._bindings, this._conn, r);
      }
      async insertArrowTable(e, r) {
        let t = p.tableToIPC(e, "stream");
        await this.insertArrowFromIPCStream(t, r);
      }
      async insertArrowFromIPCStream(e, r) {
        await this._bindings.insertArrowFromIPCStream(this._conn, e, r);
      }
      async insertCSVFromPath(e, r) {
        await this._bindings.insertCSVFromPath(this._conn, e, r);
      }
      async insertJSONFromPath(e, r) {
        await this._bindings.insertJSONFromPath(this._conn, e, r);
      }
    };
    var b = class {
      constructor(e, r, t) {
        this.db = e;
        this.conn = r;
        this.header = t;
        this._first = true, this._depleted = false, this._inFlight = null;
      }
      async next() {
        if (this._first) return this._first = false, { done: false, value: this.header };
        if (this._depleted) return { done: true, value: null };
        let e = null;
        for (this._inFlight != null && (e = await this._inFlight, this._inFlight = null); e == null; ) e = await this.db.fetchQueryResults(this.conn);
        return this._depleted = e.length == 0, this._depleted || (this._inFlight = this.db.fetchQueryResults(this.conn)), { done: this._depleted, value: e };
      }
      [Symbol.asyncIterator]() {
        return this;
      }
    };
    var y = class {
      constructor(e, r, t) {
        this.bindings = e, this.connectionId = r, this.statementId = t;
      }
      async close() {
        await this.bindings.closePrepared(this.connectionId, this.statementId);
      }
      async query(...e) {
        let r = await this.bindings.runPrepared(this.connectionId, this.statementId, e), t = p.RecordBatchReader.from(r);
        return console.assert(t.isSync()), console.assert(t.isFile()), new p.Table(t);
      }
      async send(...e) {
        let r = await this.bindings.sendPrepared(this.connectionId, this.statementId, e), t = new b(this.bindings, this.connectionId, r), n = await p.RecordBatchReader.from(t);
        return console.assert(n.isAsync()), console.assert(n.isStream()), n;
      }
    };
    var D = ((l) => (l.CANCEL_PENDING_QUERY = "CANCEL_PENDING_QUERY", l.CLOSE_PREPARED = "CLOSE_PREPARED", l.COLLECT_FILE_STATISTICS = "COLLECT_FILE_STATISTICS", l.REGISTER_OPFS_FILE_NAME = "REGISTER_OPFS_FILE_NAME", l.CONNECT = "CONNECT", l.COPY_FILE_TO_BUFFER = "COPY_FILE_TO_BUFFER", l.COPY_FILE_TO_PATH = "COPY_FILE_TO_PATH", l.CREATE_PREPARED = "CREATE_PREPARED", l.DISCONNECT = "DISCONNECT", l.DROP_FILE = "DROP_FILE", l.DROP_FILES = "DROP_FILES", l.EXPORT_FILE_STATISTICS = "EXPORT_FILE_STATISTICS", l.FETCH_QUERY_RESULTS = "FETCH_QUERY_RESULTS", l.FLUSH_FILES = "FLUSH_FILES", l.GET_FEATURE_FLAGS = "GET_FEATURE_FLAGS", l.GET_TABLE_NAMES = "GET_TABLE_NAMES", l.GET_VERSION = "GET_VERSION", l.GLOB_FILE_INFOS = "GLOB_FILE_INFOS", l.INSERT_ARROW_FROM_IPC_STREAM = "INSERT_ARROW_FROM_IPC_STREAM", l.INSERT_CSV_FROM_PATH = "IMPORT_CSV_FROM_PATH", l.INSERT_JSON_FROM_PATH = "IMPORT_JSON_FROM_PATH", l.INSTANTIATE = "INSTANTIATE", l.OPEN = "OPEN", l.PING = "PING", l.POLL_PENDING_QUERY = "POLL_PENDING_QUERY", l.REGISTER_FILE_BUFFER = "REGISTER_FILE_BUFFER", l.REGISTER_FILE_HANDLE = "REGISTER_FILE_HANDLE", l.REGISTER_FILE_URL = "REGISTER_FILE_URL", l.RESET = "RESET", l.RUN_PREPARED = "RUN_PREPARED", l.RUN_QUERY = "RUN_QUERY", l.SEND_PREPARED = "SEND_PREPARED", l.START_PENDING_QUERY = "START_PENDING_QUERY", l.TOKENIZE = "TOKENIZE", l))(D || {});
    var O = ((d) => (d.CONNECTION_INFO = "CONNECTION_INFO", d.ERROR = "ERROR", d.FEATURE_FLAGS = "FEATURE_FLAGS", d.FILE_BUFFER = "FILE_BUFFER", d.FILE_INFOS = "FILE_INFOS", d.FILE_SIZE = "FILE_SIZE", d.FILE_STATISTICS = "FILE_STATISTICS", d.INSTANTIATE_PROGRESS = "INSTANTIATE_PROGRESS", d.LOG = "LOG", d.PROGRESS_UPDATE = "PROGRESS_UPDATE", d.OK = "OK", d.PREPARED_STATEMENT_ID = "PREPARED_STATEMENT_ID", d.QUERY_PLAN = "QUERY_PLAN", d.QUERY_RESULT = "QUERY_RESULT", d.QUERY_RESULT_CHUNK = "QUERY_RESULT_CHUNK", d.QUERY_RESULT_HEADER = "QUERY_RESULT_HEADER", d.QUERY_RESULT_HEADER_OR_NULL = "QUERY_RESULT_HEADER_OR_NULL", d.REGISTERED_FILE = "REGISTERED_FILE", d.SCRIPT_TOKENS = "SCRIPT_TOKENS", d.SUCCESS = "SUCCESS", d.TABLE_NAMES = "TABLE_NAMES", d.VERSION_STRING = "VERSION_STRING", d))(O || {});
    var i = class {
      constructor(e, r) {
        this.promiseResolver = () => {
        };
        this.promiseRejecter = () => {
        };
        this.type = e, this.data = r, this.promise = new Promise((t, n) => {
          this.promiseResolver = t, this.promiseRejecter = n;
        });
      }
    };
    var c = N(require_Arrow_node());
    function k(s) {
      switch (s.typeId) {
        case c.Type.Binary:
          return { sqlType: "binary" };
        case c.Type.Bool:
          return { sqlType: "bool" };
        case c.Type.Date:
          return { sqlType: "date" };
        case c.Type.DateDay:
          return { sqlType: "date32[d]" };
        case c.Type.DateMillisecond:
          return { sqlType: "date64[ms]" };
        case c.Type.Decimal: {
          let e = s;
          return { sqlType: "decimal", precision: e.precision, scale: e.scale };
        }
        case c.Type.Float:
          return { sqlType: "float" };
        case c.Type.Float16:
          return { sqlType: "float16" };
        case c.Type.Float32:
          return { sqlType: "float32" };
        case c.Type.Float64:
          return { sqlType: "float64" };
        case c.Type.Int:
          return { sqlType: "int32" };
        case c.Type.Int16:
          return { sqlType: "int16" };
        case c.Type.Int32:
          return { sqlType: "int32" };
        case c.Type.Int64:
          return { sqlType: "int64" };
        case c.Type.Uint16:
          return { sqlType: "uint16" };
        case c.Type.Uint32:
          return { sqlType: "uint32" };
        case c.Type.Uint64:
          return { sqlType: "uint64" };
        case c.Type.Uint8:
          return { sqlType: "uint8" };
        case c.Type.IntervalDayTime:
          return { sqlType: "interval[dt]" };
        case c.Type.IntervalYearMonth:
          return { sqlType: "interval[m]" };
        case c.Type.List:
          return { sqlType: "list", valueType: k(s.valueType) };
        case c.Type.FixedSizeBinary:
          return { sqlType: "fixedsizebinary", byteWidth: s.byteWidth };
        case c.Type.Null:
          return { sqlType: "null" };
        case c.Type.Utf8:
          return { sqlType: "utf8" };
        case c.Type.Struct:
          return { sqlType: "struct", fields: s.children.map((r) => S(r.name, r.type)) };
        case c.Type.Map: {
          let e = s;
          return { sqlType: "map", keyType: k(e.keyType), valueType: k(e.valueType) };
        }
        case c.Type.Time:
          return { sqlType: "time[s]" };
        case c.Type.TimeMicrosecond:
          return { sqlType: "time[us]" };
        case c.Type.TimeMillisecond:
          return { sqlType: "time[ms]" };
        case c.Type.TimeNanosecond:
          return { sqlType: "time[ns]" };
        case c.Type.TimeSecond:
          return { sqlType: "time[s]" };
        case c.Type.Timestamp:
          return { sqlType: "timestamp", timezone: s.timezone || void 0 };
        case c.Type.TimestampSecond:
          return { sqlType: "timestamp[s]", timezone: s.timezone || void 0 };
        case c.Type.TimestampMicrosecond:
          return { sqlType: "timestamp[us]", timezone: s.timezone || void 0 };
        case c.Type.TimestampNanosecond:
          return { sqlType: "timestamp[ns]", timezone: s.timezone || void 0 };
        case c.Type.TimestampMillisecond:
          return { sqlType: "timestamp[ms]", timezone: s.timezone || void 0 };
      }
      throw new Error(`unsupported arrow type: ${s.toString()}`);
    }
    function S(s, e) {
      let r = k(e);
      return r.name = s, r;
    }
    var fe = /'(opfs:\/\/\S*?)'/g;
    var Ae = /(opfs:\/\/\S*?)/g;
    function ee(s) {
      return s.search(Ae) > -1;
    }
    function re(s) {
      return [...s.matchAll(fe)].map((e) => e[1]);
    }
    var De = new TextEncoder();
    var L = class {
      constructor(e, r = null) {
        this._onInstantiationProgress = [];
        this._onExecutionProgress = [];
        this._worker = null;
        this._workerShutdownPromise = null;
        this._workerShutdownResolver = () => {
        };
        this._nextMessageId = 0;
        this._pendingRequests = /* @__PURE__ */ new Map();
        this._config = {};
        this._logger = e, this._onMessageHandler = this.onMessage.bind(this), this._onErrorHandler = this.onError.bind(this), this._onCloseHandler = this.onClose.bind(this), r != null && this.attach(r);
      }
      get logger() {
        return this._logger;
      }
      get config() {
        return this._config;
      }
      attach(e) {
        this._worker = e, this._worker.addEventListener("message", this._onMessageHandler), this._worker.addEventListener("error", this._onErrorHandler), this._worker.addEventListener("close", this._onCloseHandler), this._workerShutdownPromise = new Promise((r, t) => {
          this._workerShutdownResolver = r;
        });
      }
      detach() {
        this._worker && (this._worker.removeEventListener("message", this._onMessageHandler), this._worker.removeEventListener("error", this._onErrorHandler), this._worker.removeEventListener("close", this._onCloseHandler), this._worker = null, this._workerShutdownResolver(null), this._workerShutdownPromise = null, this._workerShutdownResolver = () => {
        });
      }
      async terminate() {
        this._worker && (this._worker.terminate(), this._worker = null, this._workerShutdownPromise = null, this._workerShutdownResolver = () => {
        });
      }
      async postTask(e, r = []) {
        if (!this._worker) {
          console.error("cannot send a message since the worker is not set!:" + e.type + "," + e.data);
          return;
        }
        let t = this._nextMessageId++;
        return this._pendingRequests.set(t, e), this._worker.postMessage({ messageId: t, type: e.type, data: e.data }, r), await e.promise;
      }
      onMessage(e) {
        var n;
        let r = e.data;
        switch (r.type) {
          case "PROGRESS_UPDATE": {
            for (let a of this._onExecutionProgress) a(r.data);
            return;
          }
          case "LOG": {
            this._logger.log(r.data);
            return;
          }
          case "INSTANTIATE_PROGRESS": {
            for (let a of this._onInstantiationProgress) a(r.data);
            return;
          }
        }
        let t = this._pendingRequests.get(r.requestId);
        if (!t) {
          console.warn(`unassociated response: [${r.requestId}, ${r.type.toString()}]`);
          return;
        }
        if (this._pendingRequests.delete(r.requestId), r.type == "ERROR") {
          let a = new Error(r.data.message);
          a.name = r.data.name, (n = Object.getOwnPropertyDescriptor(a, "stack")) != null && n.writable && (a.stack = r.data.stack), t.promiseRejecter(a);
          return;
        }
        switch (t.type) {
          case "CLOSE_PREPARED":
          case "COLLECT_FILE_STATISTICS":
          case "REGISTER_OPFS_FILE_NAME":
          case "COPY_FILE_TO_PATH":
          case "DISCONNECT":
          case "DROP_FILE":
          case "DROP_FILES":
          case "FLUSH_FILES":
          case "INSERT_ARROW_FROM_IPC_STREAM":
          case "IMPORT_CSV_FROM_PATH":
          case "IMPORT_JSON_FROM_PATH":
          case "OPEN":
          case "PING":
          case "REGISTER_FILE_BUFFER":
          case "REGISTER_FILE_HANDLE":
          case "REGISTER_FILE_URL":
          case "RESET":
            if (r.type == "OK") {
              t.promiseResolver(r.data);
              return;
            }
            break;
          case "INSTANTIATE":
            if (this._onInstantiationProgress = [], r.type == "OK") {
              t.promiseResolver(r.data);
              return;
            }
            break;
          case "GLOB_FILE_INFOS":
            if (r.type == "FILE_INFOS") {
              t.promiseResolver(r.data);
              return;
            }
            break;
          case "GET_VERSION":
            if (r.type == "VERSION_STRING") {
              t.promiseResolver(r.data);
              return;
            }
            break;
          case "GET_FEATURE_FLAGS":
            if (r.type == "FEATURE_FLAGS") {
              t.promiseResolver(r.data);
              return;
            }
            break;
          case "GET_TABLE_NAMES":
            if (r.type == "TABLE_NAMES") {
              t.promiseResolver(r.data);
              return;
            }
            break;
          case "TOKENIZE":
            if (r.type == "SCRIPT_TOKENS") {
              t.promiseResolver(r.data);
              return;
            }
            break;
          case "COPY_FILE_TO_BUFFER":
            if (r.type == "FILE_BUFFER") {
              t.promiseResolver(r.data);
              return;
            }
            break;
          case "EXPORT_FILE_STATISTICS":
            if (r.type == "FILE_STATISTICS") {
              t.promiseResolver(r.data);
              return;
            }
            break;
          case "CONNECT":
            if (r.type == "CONNECTION_INFO") {
              t.promiseResolver(r.data);
              return;
            }
            break;
          case "RUN_PREPARED":
          case "RUN_QUERY":
            if (r.type == "QUERY_RESULT") {
              t.promiseResolver(r.data);
              return;
            }
            break;
          case "SEND_PREPARED":
            if (r.type == "QUERY_RESULT_HEADER") {
              t.promiseResolver(r.data);
              return;
            }
            break;
          case "START_PENDING_QUERY":
            if (r.type == "QUERY_RESULT_HEADER_OR_NULL") {
              t.promiseResolver(r.data);
              return;
            }
            break;
          case "POLL_PENDING_QUERY":
            if (r.type == "QUERY_RESULT_HEADER_OR_NULL") {
              t.promiseResolver(r.data);
              return;
            }
            break;
          case "CANCEL_PENDING_QUERY":
            if (this._onInstantiationProgress = [], r.type == "SUCCESS") {
              t.promiseResolver(r.data);
              return;
            }
            break;
          case "FETCH_QUERY_RESULTS":
            if (r.type == "QUERY_RESULT_CHUNK") {
              t.promiseResolver(r.data);
              return;
            }
            break;
          case "CREATE_PREPARED":
            if (r.type == "PREPARED_STATEMENT_ID") {
              t.promiseResolver(r.data);
              return;
            }
            break;
        }
        t.promiseRejecter(new Error(`unexpected response type: ${r.type.toString()}`));
      }
      onError(e) {
        console.error(e), console.error(`error in duckdb worker: ${e.message}`), this._pendingRequests.clear();
      }
      onClose() {
        if (this._workerShutdownResolver(null), this._pendingRequests.size != 0) {
          console.warn(`worker terminated with ${this._pendingRequests.size} pending requests`);
          return;
        }
        this._pendingRequests.clear();
      }
      isDetached() {
        return !this._worker;
      }
      async reset() {
        let e = new i("RESET", null);
        return await this.postTask(e);
      }
      async ping() {
        let e = new i("PING", null);
        await this.postTask(e);
      }
      async dropFile(e) {
        let r = new i("DROP_FILE", e);
        return await this.postTask(r);
      }
      async dropFiles(e) {
        let r = new i("DROP_FILES", e);
        return await this.postTask(r);
      }
      async flushFiles() {
        let e = new i("FLUSH_FILES", null);
        return await this.postTask(e);
      }
      async instantiate(e, r = null, t = (n) => {
      }) {
        this._onInstantiationProgress.push(t);
        let n = new i("INSTANTIATE", [e, r]);
        return await this.postTask(n);
      }
      async getVersion() {
        let e = new i("GET_VERSION", null);
        return await this.postTask(e);
      }
      async getFeatureFlags() {
        let e = new i("GET_FEATURE_FLAGS", null);
        return await this.postTask(e);
      }
      async open(e) {
        this._config = e;
        let r = new i("OPEN", e);
        await this.postTask(r);
      }
      async tokenize(e) {
        let r = new i("TOKENIZE", e);
        return await this.postTask(r);
      }
      async connectInternal() {
        let e = new i("CONNECT", null);
        return await this.postTask(e);
      }
      async connect() {
        let e = await this.connectInternal();
        return new T(this, e);
      }
      async disconnect(e) {
        let r = new i("DISCONNECT", e);
        await this.postTask(r);
      }
      async runQuery(e, r) {
        if (this.shouldOPFSFileHandling()) {
          let t = await this.registerOPFSFileFromSQL(r);
          try {
            return await this._runQueryAsync(e, r);
          } finally {
            t.length > 0 && await this.dropFiles(t);
          }
        } else return await this._runQueryAsync(e, r);
      }
      async _runQueryAsync(e, r) {
        let t = new i("RUN_QUERY", [e, r]);
        return await this.postTask(t);
      }
      async startPendingQuery(e, r, t = false) {
        if (this.shouldOPFSFileHandling()) {
          let n = await this.registerOPFSFileFromSQL(r);
          try {
            return await this._startPendingQueryAsync(e, r, t);
          } finally {
            n.length > 0 && await this.dropFiles(n);
          }
        } else return await this._startPendingQueryAsync(e, r, t);
      }
      async _startPendingQueryAsync(e, r, t = false) {
        let n = new i("START_PENDING_QUERY", [e, r, t]);
        return await this.postTask(n);
      }
      async pollPendingQuery(e) {
        let r = new i("POLL_PENDING_QUERY", e);
        return await this.postTask(r);
      }
      async cancelPendingQuery(e) {
        let r = new i("CANCEL_PENDING_QUERY", e);
        return await this.postTask(r);
      }
      async fetchQueryResults(e) {
        let r = new i("FETCH_QUERY_RESULTS", e);
        return await this.postTask(r);
      }
      async getTableNames(e, r) {
        let t = new i("GET_TABLE_NAMES", [e, r]);
        return await this.postTask(t);
      }
      async createPrepared(e, r) {
        let t = new i("CREATE_PREPARED", [e, r]);
        return await this.postTask(t);
      }
      async closePrepared(e, r) {
        let t = new i("CLOSE_PREPARED", [e, r]);
        await this.postTask(t);
      }
      async runPrepared(e, r, t) {
        let n = new i("RUN_PREPARED", [e, r, t]);
        return await this.postTask(n);
      }
      async sendPrepared(e, r, t) {
        let n = new i("SEND_PREPARED", [e, r, t]);
        return await this.postTask(n);
      }
      async globFiles(e) {
        let r = new i("GLOB_FILE_INFOS", e);
        return await this.postTask(r);
      }
      async registerFileText(e, r) {
        let t = De.encode(r);
        await this.registerFileBuffer(e, t);
      }
      async registerFileURL(e, r, t, n) {
        r === void 0 && (r = e);
        let a = new i("REGISTER_FILE_URL", [e, r, t, n]);
        await this.postTask(a);
      }
      async registerEmptyFileBuffer(e) {
      }
      async registerFileBuffer(e, r) {
        let t = new i("REGISTER_FILE_BUFFER", [e, r]);
        await this.postTask(t, [r.buffer]);
      }
      async registerFileHandle(e, r, t, n) {
        let a = new i("REGISTER_FILE_HANDLE", [e, r, t, n]);
        await this.postTask(a, []);
      }
      async registerOPFSFileName(e) {
        let r = new i("REGISTER_OPFS_FILE_NAME", [e]);
        await this.postTask(r, []);
      }
      async collectFileStatistics(e, r) {
        let t = new i("COLLECT_FILE_STATISTICS", [e, r]);
        await this.postTask(t, []);
      }
      async exportFileStatistics(e) {
        let r = new i("EXPORT_FILE_STATISTICS", e);
        return await this.postTask(r, []);
      }
      async copyFileToBuffer(e) {
        let r = new i("COPY_FILE_TO_BUFFER", e);
        return await this.postTask(r);
      }
      async copyFileToPath(e, r) {
        let t = new i("COPY_FILE_TO_PATH", [e, r]);
        await this.postTask(t);
      }
      async insertArrowFromIPCStream(e, r, t) {
        if (r.length == 0) return;
        let n = new i("INSERT_ARROW_FROM_IPC_STREAM", [e, r, t]);
        await this.postTask(n, [r.buffer]);
      }
      async insertCSVFromPath(e, r, t) {
        if (t.columns !== void 0) {
          let a = [];
          for (let o in t.columns) {
            let E = t.columns[o];
            a.push(S(o, E));
          }
          t.columnsFlat = a, delete t.columns;
        }
        let n = new i("IMPORT_CSV_FROM_PATH", [e, r, t]);
        await this.postTask(n);
      }
      async insertJSONFromPath(e, r, t) {
        if (t.columns !== void 0) {
          let a = [];
          for (let o in t.columns) {
            let E = t.columns[o];
            a.push(S(o, E));
          }
          t.columnsFlat = a, delete t.columns;
        }
        let n = new i("IMPORT_JSON_FROM_PATH", [e, r, t]);
        await this.postTask(n);
      }
      shouldOPFSFileHandling() {
        var e;
        return ee(this.config.path ?? "") ? ((e = this.config.opfs) == null ? void 0 : e.fileHandling) == "auto" : false;
      }
      async registerOPFSFileFromSQL(e) {
        let r = re(e), t = [];
        for (let n of r) try {
          await this.registerOPFSFileName(n), t.push(n);
        } catch (a) {
          throw console.error(a), new Error("File Not found:" + n);
        }
        return t;
      }
    };
    function Oe() {
      let s = new TextDecoder();
      return (e) => (typeof SharedArrayBuffer < "u" && e.buffer instanceof SharedArrayBuffer && (e = new Uint8Array(e)), s.decode(e));
    }
    var ur = Oe();
    var F = ((o) => (o[o.BUFFER = 0] = "BUFFER", o[o.NODE_FS = 1] = "NODE_FS", o[o.BROWSER_FILEREADER = 2] = "BROWSER_FILEREADER", o[o.BROWSER_FSACCESS = 3] = "BROWSER_FSACCESS", o[o.HTTP = 4] = "HTTP", o[o.S3 = 5] = "S3", o))(F || {});
    var w = class {
      constructor() {
        this._bindings = null;
        this._nextMessageId = 0;
      }
      log(e) {
        this.postMessage({ messageId: this._nextMessageId++, requestId: 0, type: "LOG", data: e }, []);
      }
      sendOK(e) {
        this.postMessage({ messageId: this._nextMessageId++, requestId: e.messageId, type: "OK", data: null }, []);
      }
      failWith(e, r) {
        let t = { name: r.name, message: r.message, stack: r.stack || void 0 };
        this.postMessage({ messageId: this._nextMessageId++, requestId: e.messageId, type: "ERROR", data: t }, []);
      }
      async onMessage(e) {
        switch (e.type) {
          case "PING":
            this.sendOK(e);
            return;
          case "INSTANTIATE":
            this._bindings != null && this.failWith(e, new Error("duckdb already initialized"));
            try {
              this._bindings = await this.instantiate(e.data[0], e.data[1], (r) => {
                this.postMessage({ messageId: this._nextMessageId++, requestId: e.messageId, type: "INSTANTIATE_PROGRESS", data: r }, []);
              }), this.sendOK(e);
            } catch (r) {
              console.log(r), this._bindings = null, this.failWith(e, r);
            }
            return;
          default:
            break;
        }
        if (!this._bindings) return this.failWith(e, new Error("duckdb is not initialized"));
        try {
          switch (e.type) {
            case "GET_VERSION":
              this.postMessage({ messageId: this._nextMessageId++, requestId: e.messageId, type: "VERSION_STRING", data: this._bindings.getVersion() }, []);
              break;
            case "GET_FEATURE_FLAGS":
              this.postMessage({ messageId: this._nextMessageId++, requestId: e.messageId, type: "FEATURE_FLAGS", data: this._bindings.getFeatureFlags() }, []);
              break;
            case "RESET":
              this._bindings.reset(), this.sendOK(e);
              break;
            case "OPEN": {
              let r = e.data.path;
              r != null && r.startsWith("opfs://") && (await this._bindings.prepareDBFileHandle(r, 3), e.data.useDirectIO = true), this._bindings.open(e.data), this.sendOK(e);
              break;
            }
            case "DROP_FILE":
              this._bindings.dropFile(e.data), this.sendOK(e);
              break;
            case "DROP_FILES":
              this._bindings.dropFiles(e.data), this.sendOK(e);
              break;
            case "FLUSH_FILES":
              this._bindings.flushFiles(), this.sendOK(e);
              break;
            case "CONNECT": {
              let r = this._bindings.connect();
              this.postMessage({ messageId: this._nextMessageId++, requestId: e.messageId, type: "CONNECTION_INFO", data: r.useUnsafe((t, n) => n) }, []);
              break;
            }
            case "DISCONNECT":
              this._bindings.disconnect(e.data), this.sendOK(e);
              break;
            case "CREATE_PREPARED": {
              let r = this._bindings.createPrepared(e.data[0], e.data[1]);
              this.postMessage({ messageId: this._nextMessageId++, requestId: e.messageId, type: "PREPARED_STATEMENT_ID", data: r }, []);
              break;
            }
            case "CLOSE_PREPARED": {
              this._bindings.closePrepared(e.data[0], e.data[1]), this.sendOK(e);
              break;
            }
            case "RUN_PREPARED": {
              let r = this._bindings.runPrepared(e.data[0], e.data[1], e.data[2]);
              this.postMessage({ messageId: this._nextMessageId++, requestId: e.messageId, type: "QUERY_RESULT", data: r }, [r.buffer]);
              break;
            }
            case "RUN_QUERY": {
              let r = this._bindings.runQuery(e.data[0], e.data[1]);
              this.postMessage({ messageId: this._nextMessageId++, requestId: e.messageId, type: "QUERY_RESULT", data: r }, [r.buffer]);
              break;
            }
            case "SEND_PREPARED": {
              let r = this._bindings.sendPrepared(e.data[0], e.data[1], e.data[2]);
              this.postMessage({ messageId: this._nextMessageId++, requestId: e.messageId, type: "QUERY_RESULT_HEADER", data: r }, [r.buffer]);
              break;
            }
            case "START_PENDING_QUERY": {
              let r = this._bindings.startPendingQuery(e.data[0], e.data[1], e.data[2]), t = [];
              r && t.push(r.buffer), this.postMessage({ messageId: this._nextMessageId++, requestId: e.messageId, type: "QUERY_RESULT_HEADER_OR_NULL", data: r }, t);
              break;
            }
            case "POLL_PENDING_QUERY": {
              let r = this._bindings.pollPendingQuery(e.data), t = [];
              r && t.push(r.buffer), this.postMessage({ messageId: this._nextMessageId++, requestId: e.messageId, type: "QUERY_RESULT_HEADER_OR_NULL", data: r }, t);
              break;
            }
            case "CANCEL_PENDING_QUERY": {
              let r = this._bindings.cancelPendingQuery(e.data);
              this.postMessage({ messageId: this._nextMessageId++, requestId: e.messageId, type: "SUCCESS", data: r }, []);
              break;
            }
            case "FETCH_QUERY_RESULTS": {
              let r = this._bindings.fetchQueryResults(e.data), t = r ? [r.buffer] : [];
              this.postMessage({ messageId: this._nextMessageId++, requestId: e.messageId, type: "QUERY_RESULT_CHUNK", data: r }, t);
              break;
            }
            case "GET_TABLE_NAMES": {
              let r = this._bindings.getTableNames(e.data[0], e.data[1]);
              this.postMessage({ messageId: this._nextMessageId++, requestId: e.messageId, type: "TABLE_NAMES", data: r }, []);
              break;
            }
            case "GLOB_FILE_INFOS": {
              let r = this._bindings.globFiles(e.data);
              this.postMessage({ messageId: this._nextMessageId++, requestId: e.messageId, type: "FILE_INFOS", data: r }, []);
              break;
            }
            case "REGISTER_FILE_URL":
              this._bindings.registerFileURL(e.data[0], e.data[1], e.data[2], e.data[3]), this.sendOK(e);
              break;
            case "REGISTER_FILE_BUFFER":
              this._bindings.registerFileBuffer(e.data[0], e.data[1]), this.sendOK(e);
              break;
            case "REGISTER_FILE_HANDLE":
              await this._bindings.registerFileHandleAsync(e.data[0], e.data[1], e.data[2], e.data[3]), this.sendOK(e);
              break;
            case "COPY_FILE_TO_PATH":
              this._bindings.copyFileToPath(e.data[0], e.data[1]), this.sendOK(e);
              break;
            case "COPY_FILE_TO_BUFFER": {
              let r = this._bindings.copyFileToBuffer(e.data);
              this.postMessage({ messageId: this._nextMessageId++, requestId: e.messageId, type: "FILE_BUFFER", data: r }, []);
              break;
            }
            case "COLLECT_FILE_STATISTICS":
              this._bindings.collectFileStatistics(e.data[0], e.data[1]), this.sendOK(e);
              break;
            case "REGISTER_OPFS_FILE_NAME":
              await this._bindings.registerOPFSFileName(e.data[0]), this.sendOK(e);
              break;
            case "EXPORT_FILE_STATISTICS": {
              this.postMessage({ messageId: this._nextMessageId++, requestId: e.messageId, type: "FILE_STATISTICS", data: this._bindings.exportFileStatistics(e.data) }, []);
              break;
            }
            case "INSERT_ARROW_FROM_IPC_STREAM": {
              this._bindings.insertArrowFromIPCStream(e.data[0], e.data[1], e.data[2]), this.sendOK(e);
              break;
            }
            case "IMPORT_CSV_FROM_PATH": {
              this._bindings.insertCSVFromPath(e.data[0], e.data[1], e.data[2]), this.sendOK(e);
              break;
            }
            case "IMPORT_JSON_FROM_PATH": {
              this._bindings.insertJSONFromPath(e.data[0], e.data[1], e.data[2]), this.sendOK(e);
              break;
            }
            case "TOKENIZE": {
              let r = this._bindings.tokenize(e.data);
              this.postMessage({ messageId: this._nextMessageId++, requestId: e.messageId, type: "SCRIPT_TOKENS", data: r }, []);
              break;
            }
          }
        } catch (r) {
          return console.log(r), this.failWith(e, r);
        }
      }
    };
    var te = async () => WebAssembly.validate(new Uint8Array([0, 97, 115, 109, 1, 0, 0, 0, 1, 4, 1, 96, 0, 0, 3, 2, 1, 0, 5, 3, 1, 0, 1, 10, 14, 1, 12, 0, 65, 0, 65, 0, 65, 0, 252, 10, 0, 0, 11]));
    var se = async () => WebAssembly.validate(new Uint8Array([0, 97, 115, 109, 1, 0, 0, 0, 1, 4, 1, 96, 0, 0, 3, 2, 1, 0, 10, 8, 1, 6, 0, 6, 64, 25, 11, 11]));
    var ne = async () => WebAssembly.validate(new Uint8Array([0, 97, 115, 109, 1, 0, 0, 0, 1, 5, 1, 96, 0, 1, 123, 3, 2, 1, 0, 10, 10, 1, 8, 0, 65, 0, 253, 15, 253, 98, 11]));
    var oe = () => (async (s) => {
      try {
        return typeof MessageChannel < "u" && new MessageChannel().port1.postMessage(new SharedArrayBuffer(1)), WebAssembly.validate(s);
      } catch {
        return false;
      }
    })(new Uint8Array([0, 97, 115, 109, 1, 0, 0, 0, 1, 4, 1, 96, 0, 0, 3, 2, 1, 0, 5, 4, 1, 3, 1, 1, 10, 11, 1, 9, 0, 65, 0, 254, 16, 2, 0, 26, 11]));
    var h = { name: "@duckdb/duckdb-wasm", version: "1.32.0", description: "DuckDB powered by WebAssembly", license: "MIT", repository: { type: "git", url: "https://github.com/duckdb/duckdb-wasm.git" }, keywords: ["sql", "duckdb", "relational", "database", "data", "query", "wasm", "analytics", "olap", "arrow", "parquet", "json", "csv"], dependencies: { "apache-arrow": "^17.0.0" }, devDependencies: { "@types/emscripten": "^1.39.10", "@types/jasmine": "^5.1.4", "@typescript-eslint/eslint-plugin": "^6.21.0", "@typescript-eslint/parser": "^6.21.0", esbuild: "^0.20.2", eslint: "^8.57.0", "eslint-plugin-jasmine": "^4.1.3", "eslint-plugin-react": "^7.34.0", "fast-glob": "^3.3.2", jasmine: "^5.1.0", "jasmine-core": "^5.1.2", "jasmine-spec-reporter": "^7.0.0", "js-sha256": "^0.11.1", karma: "^6.4.2", "karma-chrome-launcher": "^3.2.0", "karma-coverage": "^2.2.1", "karma-firefox-launcher": "^2.1.3", "karma-jasmine": "^5.1.0", "karma-jasmine-html-reporter": "^2.1.0", "karma-sourcemap-loader": "^0.4.0", "karma-spec-reporter": "^0.0.36", "make-dir": "^4.0.0", nyc: "^15.1.0", prettier: "^3.2.5", puppeteer: "^22.8.0", rimraf: "^5.0.5", s3rver: "^3.7.1", typedoc: "^0.25.13", typescript: "^5.3.3", "wasm-feature-detect": "^1.6.1", "web-worker": "^1.2.0" }, scripts: { "build:debug": "node bundle.mjs debug && tsc --emitDeclarationOnly", "build:release": "node bundle.mjs release && tsc --emitDeclarationOnly", docs: "typedoc", format: 'prettier --write "**/*.+(js|ts)"', report: "node ./coverage.mjs", "test:node": "node --enable-source-maps ../../node_modules/jasmine/bin/jasmine ./dist/tests-node.cjs", "test:node:debug": "node --inspect-brk --enable-source-maps ../../node_modules/jasmine/bin/jasmine ./dist/tests-node.cjs", "test:node:coverage": "nyc -r json --report-dir ./coverage/node node ../../node_modules/jasmine/bin/jasmine ./dist/tests-node.cjs", "test:firefox": "karma start ./karma/tests-firefox.cjs", "test:chrome": "karma start ./karma/tests-chrome.cjs", "test:chrome:eh": "karma start ./karma/tests-chrome-eh.cjs", "test:chrome:coverage": "karma start ./karma/tests-chrome-coverage.cjs", "test:browser": "karma start ./karma/tests-all.cjs", "test:browser:debug": "karma start ./karma/tests-debug.cjs", test: "npm run test:chrome && npm run test:node", "test:coverage": "npm run test:chrome:coverage && npm run test:node:coverage && npm run report", lint: "eslint src test" }, files: ["dist", "!dist/tests-*", "!dist/duckdb-browser-mvp.worker.js.map", "!dist/types/test"], main: "dist/duckdb-browser.cjs", module: "dist/duckdb-browser.mjs", types: "dist/duckdb-browser.d.ts", jsdelivr: "dist/duckdb-browser.cjs", unpkg: "dist/duckdb-browser.mjs", sideEffects: false, browser: { fs: false, path: false, perf_hooks: false, os: false, worker_threads: false }, exports: { "./dist/duckdb-mvp.wasm": "./dist/duckdb-mvp.wasm", "./dist/duckdb-eh.wasm": "./dist/duckdb-eh.wasm", "./dist/duckdb-coi.wasm": "./dist/duckdb-coi.wasm", "./dist/duckdb-browser": "./dist/duckdb-browser.mjs", "./dist/duckdb-browser.cjs": "./dist/duckdb-browser.cjs", "./dist/duckdb-browser.mjs": "./dist/duckdb-browser.mjs", "./dist/duckdb-browser-coi.pthread.worker.js": "./dist/duckdb-browser-coi.pthread.worker.js", "./dist/duckdb-browser-coi.worker.js": "./dist/duckdb-browser-coi.worker.js", "./dist/duckdb-browser-eh.worker.js": "./dist/duckdb-browser-eh.worker.js", "./dist/duckdb-browser-mvp.worker.js": "./dist/duckdb-browser-mvp.worker.js", "./dist/duckdb-node": "./dist/duckdb-node.cjs", "./dist/duckdb-node.cjs": "./dist/duckdb-node.cjs", "./dist/duckdb-node-blocking": "./dist/duckdb-node-blocking.cjs", "./dist/duckdb-node-blocking.cjs": "./dist/duckdb-node-blocking.cjs", "./dist/duckdb-node-eh.worker.cjs": "./dist/duckdb-node-eh.worker.cjs", "./dist/duckdb-node-mvp.worker.cjs": "./dist/duckdb-node-mvp.worker.cjs", "./blocking": { node: { types: "./dist/duckdb-node-blocking.d.ts", require: "./dist/duckdb-node-blocking.cjs", import: "./dist/duckdb-node-blocking.cjs" }, types: "./dist/duckdb-node-blocking.d.ts", import: "./dist/duckdb-node-blocking.mjs", require: "./dist/duckdb-node-blocking.cjs" }, ".": { browser: { types: "./dist/duckdb-browser.d.ts", import: "./dist/duckdb-browser.mjs", require: "./dist/duckdb-browser.cjs" }, node: { types: "./dist/duckdb-node.d.ts", import: "./dist/duckdb-node.cjs", require: "./dist/duckdb-node.cjs" }, types: "./dist/duckdb-browser.d.ts", import: "./dist/duckdb-browser.mjs", require: "./dist/duckdb-browser.cjs" } } };
    var U = h.name;
    var C = h.version;
    var W = h.version.split(".");
    var we = W[0];
    var Ue = W[1];
    var Ce = W[2];
    var Q = () => typeof navigator > "u";
    var ae = () => Q() ? "node" : navigator.userAgent;
    var We = () => ae().includes("Firefox");
    var ve = () => /^((?!chrome|android).)*safari/i.test(ae());
    function Me() {
      let s = `https://cdn.jsdelivr.net/npm/${U}@${C}/dist/`;
      return { mvp: { mainModule: `${s}duckdb-mvp.wasm`, mainWorker: `${s}duckdb-browser-mvp.worker.js` }, eh: { mainModule: `${s}duckdb-eh.wasm`, mainWorker: `${s}duckdb-browser-eh.worker.js` } };
    }
    var v = null;
    var M = null;
    var B = null;
    var G = null;
    var x = null;
    async function ie() {
      return v == null && (v = typeof BigInt64Array < "u"), M == null && (M = await se()), B == null && (B = await oe()), G == null && (G = await ne()), x == null && (x = await te()), { bigInt64Array: v, crossOriginIsolated: Q() || globalThis.crossOriginIsolated || false, wasmExceptions: M, wasmSIMD: G, wasmThreads: B, wasmBulkMemory: x };
    }
    async function Be(s) {
      let e = await ie();
      if (e.wasmExceptions) {
        if (e.wasmSIMD && e.wasmThreads && e.crossOriginIsolated && s.coi) return { mainModule: s.coi.mainModule, mainWorker: s.coi.mainWorker, pthreadWorker: s.coi.pthreadWorker };
        if (s.eh) return { mainModule: s.eh.mainModule, mainWorker: s.eh.mainWorker, pthreadWorker: null };
      }
      return { mainModule: s.mvp.mainModule, mainWorker: s.mvp.mainWorker, pthreadWorker: null };
    }
    var ue = N(de());
    async function Ye(s) {
      let e = new Request(s), r = await fetch(e), t = URL.createObjectURL(await r.blob());
      return new ue.default(t);
    }
  }
});

// src/index.ts
var index_exports = {};
__export(index_exports, {
  GeocoderError: () => GeocoderError,
  GeocoderNetworkError: () => GeocoderNetworkError,
  GeocoderTimeoutError: () => GeocoderTimeoutError,
  OvertureGeocoder: () => OvertureGeocoder,
  clearCatalogCache: () => import_overturemaps2.clearCache,
  closeDuckDB: () => closeDuckDB,
  default: () => index_default,
  geocode: () => geocode,
  getLatestRelease: () => import_overturemaps2.getLatestRelease,
  getStacCatalog: () => import_overturemaps2.getStacCatalog,
  isDuckDBAvailable: () => isDuckDBAvailable,
  queryOverture: () => queryOverture,
  reverseGeocode: () => reverseGeocode
});
module.exports = __toCommonJS(index_exports);
var import_overturemaps = require("@bradrichardson/overturemaps");

// src/duckdb-query.ts
var duckdb = null;
var db = null;
var conn = null;
var initPromise = null;
var latestRelease = null;
var duckdbUnavailable = false;
async function loadDuckDB() {
  if (duckdb) return duckdb;
  if (duckdbUnavailable) {
    throw new Error(
      "DuckDB-WASM is not available. Install @duckdb/duckdb-wasm for S3 query features."
    );
  }
  try {
    const module2 = await Promise.resolve().then(() => __toESM(require_duckdb_node()));
    duckdb = module2;
    return duckdb;
  } catch {
    duckdbUnavailable = true;
    throw new Error(
      "DuckDB-WASM is not available. Install @duckdb/duckdb-wasm for S3 query features: npm install @duckdb/duckdb-wasm"
    );
  }
}
async function initDuckDB() {
  if (db) return;
  const duckdbModule = await loadDuckDB();
  const JSDELIVR_BUNDLES = duckdbModule.getJsDelivrBundles();
  const bundle = await duckdbModule.selectBundle(JSDELIVR_BUNDLES);
  const worker_url = URL.createObjectURL(
    new Blob([`importScripts("${bundle.mainWorker}");`], {
      type: "text/javascript"
    })
  );
  const worker = new Worker(worker_url);
  const logger = new duckdbModule.ConsoleLogger();
  db = new duckdbModule.AsyncDuckDB(logger, worker);
  await db.instantiate(bundle.mainModule, bundle.pthreadWorker);
  conn = await db.connect();
  await conn.query(`
    INSTALL httpfs;
    LOAD httpfs;
    INSTALL spatial;
    LOAD spatial;
    SET s3_region = 'us-west-2';
  `);
  URL.revokeObjectURL(worker_url);
}
async function getConnection() {
  if (!initPromise) {
    initPromise = initDuckDB();
  }
  await initPromise;
  return conn;
}
async function getOvertureRelease() {
  if (latestRelease) return latestRelease;
  try {
    const { getLatestRelease: getLatestRelease2 } = await import("@bradrichardson/overturemaps");
    latestRelease = await getLatestRelease2();
  } catch {
    latestRelease = "2024-11-13.0";
  }
  return latestRelease;
}
async function queryOverture(sql) {
  const conn2 = await getConnection();
  const release = await getOvertureRelease();
  const query = sql.replace(/__LATEST__/g, release);
  try {
    const result = await conn2.query(query);
    return result.toArray().map((row) => {
      const obj = {};
      for (const key of Object.keys(row)) {
        obj[key] = row[key];
      }
      return obj;
    });
  } catch (error) {
    throw new Error(
      `DuckDB query failed: ${error instanceof Error ? error.message : String(error)}`
    );
  }
}
async function closeDuckDB() {
  if (conn) {
    await conn.close();
    conn = null;
  }
  if (db) {
    await db.terminate();
    db = null;
  }
  initPromise = null;
}
function isDuckDBAvailable() {
  return db !== null;
}

// src/index.ts
var import_overturemaps2 = require("@bradrichardson/overturemaps");
var GeocoderError = class extends Error {
  constructor(message, status, response) {
    super(message);
    this.status = status;
    this.response = response;
    this.name = "GeocoderError";
  }
};
var GeocoderTimeoutError = class extends GeocoderError {
  constructor(message = "Request timed out") {
    super(message);
    this.name = "GeocoderTimeoutError";
  }
};
var GeocoderNetworkError = class extends GeocoderError {
  constructor(message, cause) {
    super(message);
    this.cause = cause;
    this.name = "GeocoderNetworkError";
  }
};
var DEFAULT_BASE_URL = "https://overture-geocoder.bradr.workers.dev";
var DEFAULT_TIMEOUT = 3e4;
var DEFAULT_RETRIES = 0;
var DEFAULT_RETRY_DELAY = 1e3;
var OvertureGeocoder = class {
  baseUrl;
  timeout;
  retries;
  retryDelay;
  headers;
  fetchFn;
  onRequest;
  onResponse;
  constructor(config = {}) {
    this.baseUrl = (config.baseUrl || DEFAULT_BASE_URL).replace(/\/$/, "");
    this.timeout = config.timeout ?? DEFAULT_TIMEOUT;
    this.retries = config.retries ?? DEFAULT_RETRIES;
    this.retryDelay = config.retryDelay ?? DEFAULT_RETRY_DELAY;
    this.headers = config.headers ?? {};
    this.fetchFn = config.fetch ?? globalThis.fetch.bind(globalThis);
    this.onRequest = config.onRequest;
    this.onResponse = config.onResponse;
  }
  /**
   * Search for addresses matching the query.
   */
  async search(query, options = {}) {
    const params = new URLSearchParams({
      q: query,
      format: options.format || "jsonv2",
      limit: String(Math.min(Math.max(1, options.limit || 10), 40))
    });
    const url = `${this.baseUrl}/search?${params}`;
    const response = await this.fetchWithRetry(url);
    const data = await response.json();
    if (options.format === "geojson") {
      return data;
    }
    return this.parseResults(data);
  }
  /**
   * Search and return results as GeoJSON FeatureCollection.
   */
  async searchGeoJSON(query, options = {}) {
    const params = new URLSearchParams({
      q: query,
      format: "geojson",
      limit: String(Math.min(Math.max(1, options.limit || 10), 40))
    });
    const url = `${this.baseUrl}/search?${params}`;
    const response = await this.fetchWithRetry(url);
    return response.json();
  }
  /**
   * Reverse geocode coordinates to divisions.
   *
   * Returns divisions (localities, neighborhoods, counties, etc.) that
   * contain the given coordinate. Results are sorted by specificity
   * (smallest/most specific first).
   *
   * @param lat Latitude
   * @param lon Longitude
   * @param options Reverse geocoding options
   * @param options.verifyGeometry If true, fetches full polygons from S3 and filters
   *                               to only results where point is inside the polygon
   */
  async reverse(lat, lon, options = {}) {
    const params = new URLSearchParams({
      lat: String(lat),
      lon: String(lon),
      format: options.format || "jsonv2"
    });
    const url = `${this.baseUrl}/reverse?${params}`;
    const response = await this.fetchWithRetry(url);
    const data = await response.json();
    if (options.format === "geojson") {
      return data;
    }
    let results = this.parseReverseResults(data);
    if (options.verifyGeometry) {
      const verifyLimit = options.verifyLimit ?? 10;
      const toVerify = results.slice(0, verifyLimit);
      const verified = await this.verifyResultsGeometry(toVerify, lat, lon);
      results = [...verified, ...results.slice(verifyLimit)];
    }
    return results;
  }
  /**
   * Verify which reverse geocode results actually contain the point.
   * Fetches full geometry from S3 and performs point-in-polygon checks.
   * Updates confidence to "exact" for verified results.
   */
  async verifyResultsGeometry(results, lat, lon) {
    const verified = [];
    const geometryPromises = results.map(async (result) => {
      try {
        const contains = await this.verifyContainsPoint(result.gers_id, lat, lon);
        return { result, contains };
      } catch {
        return { result, contains: true };
      }
    });
    const checks = await Promise.all(geometryPromises);
    for (const { result, contains } of checks) {
      if (contains) {
        verified.push({
          ...result,
          confidence: "exact"
          // Upgraded from bbox
        });
      }
    }
    return verified;
  }
  /**
   * Reverse geocode and return results as GeoJSON FeatureCollection.
   */
  async reverseGeoJSON(lat, lon) {
    const params = new URLSearchParams({
      lat: String(lat),
      lon: String(lon),
      format: "geojson"
    });
    const url = `${this.baseUrl}/reverse?${params}`;
    const response = await this.fetchWithRetry(url);
    return response.json();
  }
  /**
   * Verify if a point is inside a division's polygon.
   *
   * Fetches the full geometry from Overture S3 and performs
   * a point-in-polygon check using ray casting algorithm.
   */
  async verifyContainsPoint(gersId, lat, lon) {
    const feature = await this.getFullGeometry(gersId);
    if (!feature) return false;
    const geometry = feature.geometry;
    if (geometry.type === "Polygon") {
      return this.pointInPolygon([lon, lat], geometry.coordinates[0]);
    }
    if (geometry.type === "MultiPolygon") {
      return geometry.coordinates.some(
        (poly) => this.pointInPolygon([lon, lat], poly[0])
      );
    }
    return false;
  }
  /**
   * Get the base URL configured for this client.
   */
  getBaseUrl() {
    return this.baseUrl;
  }
  /**
   * Fetch full geometry for a GERS ID directly from Overture S3.
   *
   * Uses the @bradrichardson/overturemaps package for efficient lookup.
   *
   * @param gersId The GERS ID to look up
   * @returns GeoJSON Feature with full geometry, or null if not found
   */
  async getFullGeometry(gersId) {
    const feature = await (0, import_overturemaps.getFeatureByGersId)(gersId);
    if (!feature) return null;
    return {
      type: "Feature",
      id: feature.id,
      properties: feature.properties,
      bbox: feature.bbox,
      geometry: feature.geometry
    };
  }
  /**
   * Close all DuckDB connections and release resources.
   * Call this when done with geometry/place/address fetching to free memory.
   */
  async close() {
    await Promise.all([(0, import_overturemaps.closeDb)(), closeDuckDB()]);
  }
  // ==========================================================================
  // Overture S3 Direct Query Methods (Places, Addresses)
  // ==========================================================================
  /**
   * Get nearby places from Overture S3 using DuckDB spatial query.
   *
   * Queries the Overture places theme directly from S3 within a radius
   * of the given coordinates. Results include business names, categories,
   * addresses, and contact info.
   *
   * @param lat Latitude of center point
   * @param lon Longitude of center point
   * @param options Search options (radius, limit, category filter)
   * @returns Array of nearby places sorted by distance
   */
  async getNearbyPlaces(lat, lon, options = {}) {
    const radiusKm = options.radiusKm ?? 1;
    const limit = options.limit ?? 10;
    const category = options.category;
    const latDelta = radiusKm / 111;
    const lonDelta = radiusKm / (111 * Math.cos(lat * Math.PI / 180));
    const bboxFilter = `
      bbox.xmin <= ${lon + lonDelta} AND bbox.xmax >= ${lon - lonDelta} AND
      bbox.ymin <= ${lat + latDelta} AND bbox.ymax >= ${lat - latDelta}
    `;
    const categoryFilter = category ? `AND categories.primary = '${category.replace(/'/g, "''")}'` : "";
    const query = `
      SELECT
        id,
        names,
        categories,
        addresses,
        phones,
        websites,
        brand,
        ST_X(geometry) as lon,
        ST_Y(geometry) as lat,
        -- Haversine distance calculation
        6371 * 2 * ASIN(SQRT(
          POWER(SIN((RADIANS(ST_Y(geometry)) - RADIANS(${lat})) / 2), 2) +
          COS(RADIANS(${lat})) * COS(RADIANS(ST_Y(geometry))) *
          POWER(SIN((RADIANS(ST_X(geometry)) - RADIANS(${lon})) / 2), 2)
        )) as distance_km,
        confidence
      FROM read_parquet(
        's3://overturemaps-us-west-2/release/__LATEST__/theme=places/type=place/*',
        hive_partitioning = true
      )
      WHERE ${bboxFilter}
      ${categoryFilter}
      ORDER BY distance_km ASC
      LIMIT ${limit * 2}
    `;
    try {
      const rows = await queryOverture(query);
      return rows.filter((row) => row.distance_km <= radiusKm).slice(0, limit).map((row) => ({
        id: row.id,
        names: row.names,
        categories: row.categories,
        addresses: row.addresses,
        phones: row.phones,
        websites: row.websites,
        brand: row.brand,
        lat: row.lat,
        lon: row.lon,
        distance_km: row.distance_km,
        confidence: row.confidence
      }));
    } catch (error) {
      throw new GeocoderError(
        `Failed to query nearby places: ${error instanceof Error ? error.message : String(error)}`
      );
    }
  }
  /**
   * Get nearby addresses from Overture S3 using DuckDB spatial query.
   *
   * Queries the Overture addresses theme directly from S3 within a radius
   * of the given coordinates. Returns structured address components.
   *
   * @param lat Latitude of center point
   * @param lon Longitude of center point
   * @param options Search options (radius, limit)
   * @returns Array of nearby addresses sorted by distance
   */
  async getNearbyAddresses(lat, lon, options = {}) {
    const radiusKm = options.radiusKm ?? 0.5;
    const limit = options.limit ?? 10;
    const latDelta = radiusKm / 111;
    const lonDelta = radiusKm / (111 * Math.cos(lat * Math.PI / 180));
    const query = `
      SELECT
        id,
        number,
        street,
        unit,
        postcode,
        freeform,
        ST_X(geometry) as lon,
        ST_Y(geometry) as lat,
        -- Haversine distance calculation
        6371 * 2 * ASIN(SQRT(
          POWER(SIN((RADIANS(ST_Y(geometry)) - RADIANS(${lat})) / 2), 2) +
          COS(RADIANS(${lat})) * COS(RADIANS(ST_Y(geometry))) *
          POWER(SIN((RADIANS(ST_X(geometry)) - RADIANS(${lon})) / 2), 2)
        )) as distance_km
      FROM read_parquet(
        's3://overturemaps-us-west-2/release/__LATEST__/theme=addresses/type=address/*',
        hive_partitioning = true
      )
      WHERE bbox.xmin <= ${lon + lonDelta} AND bbox.xmax >= ${lon - lonDelta}
        AND bbox.ymin <= ${lat + latDelta} AND bbox.ymax >= ${lat - latDelta}
      ORDER BY distance_km ASC
      LIMIT ${limit * 2}
    `;
    try {
      const rows = await queryOverture(query);
      return rows.filter((row) => row.distance_km <= radiusKm).slice(0, limit).map((row) => ({
        id: row.id,
        number: row.number,
        street: row.street,
        unit: row.unit,
        postcode: row.postcode,
        freeform: row.freeform,
        lat: row.lat,
        lon: row.lon,
        distance_km: row.distance_km
      }));
    } catch (error) {
      throw new GeocoderError(
        `Failed to query nearby addresses: ${error instanceof Error ? error.message : String(error)}`
      );
    }
  }
  /**
   * Combined reverse geocode with optional geometry verification and
   * nearby places/addresses lookup.
   *
   * This is a convenience method that combines:
   * 1. Reverse geocoding (division hierarchy)
   * 2. Optional point-in-polygon verification
   * 3. Nearby places from Overture S3
   * 4. Nearby addresses from Overture S3
   *
   * @param lat Latitude
   * @param lon Longitude
   * @param options Configuration for what to include
   * @returns Combined result with divisions, places, and addresses
   */
  async reverseAndRefine(lat, lon, options = {}) {
    const {
      verifyGeometry = true,
      includePlaces = true,
      includeAddresses = true,
      radiusKm = 0.5,
      nearbyLimit = 5,
      placeCategory
    } = options;
    const [divisions, places, addresses] = await Promise.all([
      this.reverse(lat, lon, { verifyGeometry }),
      includePlaces ? this.getNearbyPlaces(lat, lon, {
        radiusKm,
        limit: nearbyLimit,
        category: placeCategory
      }) : Promise.resolve(void 0),
      includeAddresses ? this.getNearbyAddresses(lat, lon, {
        radiusKm,
        limit: nearbyLimit
      }) : Promise.resolve(void 0)
    ]);
    return {
      divisions,
      places: places ?? void 0,
      addresses: addresses ?? void 0
    };
  }
  // ==========================================================================
  // Private methods
  // ==========================================================================
  async fetchWithRetry(url, attempt = 0) {
    try {
      const response = await this.doFetch(url);
      if (!response.ok) {
        if (response.status >= 400 && response.status < 500) {
          throw new GeocoderError(
            `Request failed: ${response.status} ${response.statusText}`,
            response.status,
            response
          );
        }
        if (attempt < this.retries) {
          await this.delay(this.retryDelay);
          return this.fetchWithRetry(url, attempt + 1);
        }
        throw new GeocoderError(
          `Request failed after ${attempt + 1} attempts: ${response.status} ${response.statusText}`,
          response.status,
          response
        );
      }
      return response;
    } catch (error) {
      if (error instanceof GeocoderError) throw error;
      if (error instanceof Error) {
        if (error.name === "AbortError") {
          if (attempt < this.retries) {
            await this.delay(this.retryDelay);
            return this.fetchWithRetry(url, attempt + 1);
          }
          throw new GeocoderTimeoutError(
            `Request timed out after ${this.timeout}ms (${attempt + 1} attempts)`
          );
        }
        if (attempt < this.retries) {
          await this.delay(this.retryDelay);
          return this.fetchWithRetry(url, attempt + 1);
        }
        throw new GeocoderNetworkError(
          `Network error after ${attempt + 1} attempts: ${error.message}`,
          error
        );
      }
      throw error;
    }
  }
  async doFetch(url) {
    const controller = new AbortController();
    const timeoutId = setTimeout(() => controller.abort(), this.timeout);
    try {
      let init = {
        method: "GET",
        headers: {
          Accept: "application/json",
          ...this.headers
        },
        signal: controller.signal
      };
      if (this.onRequest) {
        init = await this.onRequest(url, init);
      }
      let response = await this.fetchFn(url, init);
      if (this.onResponse) {
        response = await this.onResponse(response);
      }
      return response;
    } finally {
      clearTimeout(timeoutId);
    }
  }
  parseResults(data) {
    if (!Array.isArray(data)) return [];
    return data.map((r) => {
      const record = r;
      return {
        gers_id: record.gers_id,
        primary_name: record.primary_name,
        lat: record.lat,
        lon: record.lon,
        boundingbox: record.boundingbox,
        importance: record.importance || 0,
        type: record.type || "unknown"
      };
    });
  }
  parseReverseResults(data) {
    if (!Array.isArray(data)) return [];
    return data.map((r) => {
      const record = r;
      return {
        gers_id: record.gers_id,
        primary_name: record.primary_name,
        subtype: record.subtype,
        lat: record.lat,
        lon: record.lon,
        boundingbox: record.boundingbox,
        distance_km: record.distance_km,
        confidence: record.confidence,
        hierarchy: record.hierarchy
      };
    });
  }
  pointInPolygon(point, ring) {
    let inside = false;
    const [x, y] = point;
    for (let i = 0, j = ring.length - 1; i < ring.length; j = i++) {
      const [xi, yi] = ring[i];
      const [xj, yj] = ring[j];
      if (yi > y !== yj > y && x < (xj - xi) * (y - yi) / (yj - yi) + xi) {
        inside = !inside;
      }
    }
    return inside;
  }
  delay(ms) {
    return new Promise((resolve) => setTimeout(resolve, ms));
  }
};
async function geocode(query, options) {
  const client = new OvertureGeocoder();
  return client.search(query, options);
}
async function reverseGeocode(lat, lon, options) {
  const client = new OvertureGeocoder();
  return client.reverse(lat, lon, options);
}
var index_default = OvertureGeocoder;
// Annotate the CommonJS export names for ESM import in node:
0 && (module.exports = {
  GeocoderError,
  GeocoderNetworkError,
  GeocoderTimeoutError,
  OvertureGeocoder,
  clearCatalogCache,
  closeDuckDB,
  geocode,
  getLatestRelease,
  getStacCatalog,
  isDuckDBAvailable,
  queryOverture,
  reverseGeocode
});

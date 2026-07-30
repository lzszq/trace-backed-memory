const JSON_NUMBER = /^-?(?:0|[1-9]\d*)(?:\.\d+)?(?:[eE][+-]?\d+)?/;
const HEX = /^[0-9a-fA-F]{4}$/;

export const JSON_MAX_BYTES = 8_388_608;
export const JSON_MAX_NODES = 100_000;
export const JSON_MAX_DEPTH = 100;

function invalid(message: string): never {
  throw new TypeError(message);
}

function hasUnpairedSurrogate(value: string): boolean {
  for (let index = 0; index < value.length; index += 1) {
    const code = value.charCodeAt(index);
    if (code >= 0xd800 && code <= 0xdbff) {
      const next = value.charCodeAt(index + 1);
      if (!(next >= 0xdc00 && next <= 0xdfff)) {
        return true;
      }
      index += 1;
    } else if (code >= 0xdc00 && code <= 0xdfff) {
      return true;
    }
  }
  return false;
}

function validateJsonValue(
  value: unknown,
  depth: number,
  state: { nodes: number; readonly seen: Set<object> },
): void {
  if (depth > JSON_MAX_DEPTH) {
    invalid("JSON exceeds the depth limit");
  }
  state.nodes += 1;
  if (state.nodes > JSON_MAX_NODES) {
    invalid("JSON exceeds the node limit");
  }
  if (value === null || typeof value === "boolean") {
    return;
  }
  if (typeof value === "string") {
    if (hasUnpairedSurrogate(value)) {
      invalid("JSON contains an unpaired surrogate");
    }
    return;
  }
  if (typeof value === "number") {
    if (!Number.isFinite(value)) {
      invalid("JSON numbers must be finite");
    }
    return;
  }
  if (typeof value !== "object") {
    invalid("value is not JSON data");
  }
  if (state.seen.has(value)) {
    invalid("JSON data must not be cyclic");
  }
  state.seen.add(value);
  try {
    if (Array.isArray(value)) {
      for (let index = 0; index < value.length; index += 1) {
        if (!(index in value)) {
          invalid("JSON arrays must not contain holes");
        }
        validateJsonValue(value[index], depth + 1, state);
      }
      return;
    }
    const prototype = Object.getPrototypeOf(value);
    if (prototype !== Object.prototype && prototype !== null) {
      invalid("JSON objects must be plain records");
    }
    for (const key of Reflect.ownKeys(value)) {
      if (typeof key !== "string" || hasUnpairedSurrogate(key)) {
        invalid("JSON object keys must be valid strings");
      }
      const descriptor = Object.getOwnPropertyDescriptor(value, key);
      if (
        descriptor === undefined ||
        !descriptor.enumerable ||
        !("value" in descriptor)
      ) {
        invalid("JSON object fields must be enumerable data properties");
      }
      validateJsonValue(descriptor.value, depth + 1, state);
    }
  } finally {
    state.seen.delete(value);
  }
}

export function stringifyBoundedJson(value: unknown): string {
  validateJsonValue(value, 1, { nodes: 0, seen: new Set<object>() });
  const source = JSON.stringify(value);
  if (
    source === undefined ||
    new TextEncoder().encode(source).byteLength > JSON_MAX_BYTES
  ) {
    invalid("JSON exceeds the wire size limit");
  }
  return source;
}

class Parser {
  private index = 0;
  private nodes = 0;

  constructor(private readonly source: string) {}

  parse(): unknown {
    this.skipWhitespace();
    const value = this.parseValue(1);
    this.skipWhitespace();
    if (this.index !== this.source.length) {
      invalid("JSON has trailing data");
    }
    return value;
  }

  private parseValue(depth: number): unknown {
    if (depth > JSON_MAX_DEPTH) {
      invalid("JSON exceeds the depth limit");
    }
    this.nodes += 1;
    if (this.nodes > JSON_MAX_NODES) {
      invalid("JSON exceeds the node limit");
    }
    const token = this.source[this.index];
    if (token === "{") {
      return this.parseObject(depth);
    }
    if (token === "[") {
      return this.parseArray(depth);
    }
    if (token === '"') {
      return this.parseString();
    }
    if (token === "t" && this.consumeLiteral("true")) {
      return true;
    }
    if (token === "f" && this.consumeLiteral("false")) {
      return false;
    }
    if (token === "n" && this.consumeLiteral("null")) {
      return null;
    }
    return this.parseNumber();
  }

  private parseObject(depth: number): Record<string, unknown> {
    this.index += 1;
    this.skipWhitespace();
    const value: Record<string, unknown> = Object.create(null);
    const keys = new Set<string>();
    if (this.source[this.index] === "}") {
      this.index += 1;
      return value;
    }
    for (;;) {
      if (this.source[this.index] !== '"') {
        invalid("JSON object key is invalid");
      }
      const key = this.parseString();
      if (keys.has(key)) {
        invalid("JSON object keys must be unique");
      }
      keys.add(key);
      this.skipWhitespace();
      if (this.source[this.index] !== ":") {
        invalid("JSON object separator is invalid");
      }
      this.index += 1;
      this.skipWhitespace();
      Object.defineProperty(value, key, {
        value: this.parseValue(depth + 1),
        enumerable: true,
        configurable: true,
        writable: true,
      });
      this.skipWhitespace();
      const separator = this.source[this.index];
      if (separator === "}") {
        this.index += 1;
        return value;
      }
      if (separator !== ",") {
        invalid("JSON object delimiter is invalid");
      }
      this.index += 1;
      this.skipWhitespace();
    }
  }

  private parseArray(depth: number): unknown[] {
    this.index += 1;
    this.skipWhitespace();
    const value: unknown[] = [];
    if (this.source[this.index] === "]") {
      this.index += 1;
      return value;
    }
    for (;;) {
      value.push(this.parseValue(depth + 1));
      this.skipWhitespace();
      const separator = this.source[this.index];
      if (separator === "]") {
        this.index += 1;
        return value;
      }
      if (separator !== ",") {
        invalid("JSON array delimiter is invalid");
      }
      this.index += 1;
      this.skipWhitespace();
    }
  }

  private parseString(): string {
    const start = this.index;
    this.index += 1;
    for (;;) {
      if (this.index >= this.source.length) {
        invalid("JSON string is unterminated");
      }
      const token = this.source[this.index];
      if (token === '"') {
        this.index += 1;
        const value = JSON.parse(
          this.source.slice(start, this.index),
        ) as string;
        if (hasUnpairedSurrogate(value)) {
          invalid("JSON string contains an unpaired surrogate");
        }
        return value;
      }
      if (token === "\\") {
        this.index += 1;
        const escaped = this.source[this.index];
        if (escaped === "u") {
          const digits = this.source.slice(this.index + 1, this.index + 5);
          if (!HEX.test(digits)) {
            invalid("JSON Unicode escape is invalid");
          }
          this.index += 5;
          continue;
        }
        if (
          escaped === undefined ||
          !'"\\/bfnrt'.includes(escaped)
        ) {
          invalid("JSON escape is invalid");
        }
        this.index += 1;
        continue;
      }
      if (token === undefined || token.charCodeAt(0) < 0x20) {
        invalid("JSON string contains a control character");
      }
      this.index += 1;
    }
  }

  private parseNumber(): number {
    const match = JSON_NUMBER.exec(this.source.slice(this.index));
    if (match === null) {
      invalid("JSON value is invalid");
    }
    this.index += match[0].length;
    const value = Number(match[0]);
    if (!Number.isFinite(value)) {
      invalid("JSON number is not finite");
    }
    return value;
  }

  private consumeLiteral(literal: string): boolean {
    if (!this.source.startsWith(literal, this.index)) {
      return false;
    }
    this.index += literal.length;
    return true;
  }

  private skipWhitespace(): void {
    while (
      this.source[this.index] === " " ||
      this.source[this.index] === "\n" ||
      this.source[this.index] === "\r" ||
      this.source[this.index] === "\t"
    ) {
      this.index += 1;
    }
  }
}

export function parseBoundedJson(
  bytes: Uint8Array,
  maxBytes = JSON_MAX_BYTES,
): unknown {
  if (
    !Number.isSafeInteger(maxBytes) ||
    maxBytes < 1 ||
    bytes.byteLength > maxBytes
  ) {
    invalid("JSON exceeds the wire size limit");
  }
  const source = new TextDecoder("utf-8", { fatal: true }).decode(bytes);
  return new Parser(source).parse();
}

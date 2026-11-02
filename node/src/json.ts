/**
 * JSON with lossless decimals. Decimal numbers are read as strings holding exactly the digits the
 * server sent, and written back as JSON numbers holding exactly the digits given, so amounts never
 * pass through floating point. Where decimals sit comes from shapes generated from the spec.
 */

/**
 * Where decimals sit in a value: `'#'` is a decimal, any other string names a model of the
 * {@link Shapes} table, `['array', item]` and `['map', value]` are collections, and an object
 * lists the fields of an inline object.
 */
export type Shape =
  | string
  | readonly ['array' | 'map', Shape]
  | { readonly [field: string]: Shape };

/** Fields holding decimals, by model name; models without decimals are left out. */
export type Shapes = Readonly<Record<string, Readonly<Record<string, Shape>>>>;

const DECIMAL = '#';
const JSON_NUMBER = /^-?(?:0|[1-9]\d*)(?:\.\d+)?(?:[eE][+-]?\d+)?$/;

type Reviver = (
  this: unknown,
  key: string,
  value: unknown,
  context?: { source?: string },
) => unknown;
const parseWithSource = JSON.parse as (text: string, reviver: Reviver) => unknown;
// Node.js 21+ and current browsers; older runtimes send decimals as JavaScript numbers.
const rawJSON = (JSON as { rawJSON?: (text: string) => unknown }).rawJSON;

/** A number whose source text a JavaScript number would change, such as `0.10` or 17 digits. */
class SourceNumber {
  constructor(
    readonly value: number,
    readonly source: string,
  ) {}
}

/** Reads and writes JSON, keeping decimals exact where the shapes mark them. */
export class JsonCodec {
  readonly #shapes: Shapes;

  constructor(shapes: Shapes = {}) {
    this.#shapes = shapes;
  }

  /**
   * Parses JSON without decoding it yet: numbers that a JavaScript number would change keep their
   * source until {@link JsonCodec.decode} decides what they are. Throws `SyntaxError`.
   */
  read(text: string): unknown {
    return parseWithSource(text, (_key, value, context) => {
      if (typeof value !== 'number' || context?.source === undefined) return value;
      return context.source === String(value) ? value : new SourceNumber(value, context.source);
    });
  }

  /** Finishes what {@link JsonCodec.read} returned: decimals become strings, the rest numbers. */
  decode(value: unknown, shape?: Shape): unknown {
    if (value instanceof SourceNumber) return shape === DECIMAL ? value.source : value.value;
    if (shape === DECIMAL) return typeof value === 'number' ? String(value) : value;
    if (Array.isArray(value)) {
      const item = this.#items(shape, 'array');
      for (let index = 0; index < value.length; index++) {
        value[index] = this.decode(value[index], item);
      }
    } else if (typeof value === 'object' && value !== null) {
      const object = value as Record<string, unknown>;
      const values = this.#items(shape, 'map');
      const fields = this.#fields(shape);
      for (const key of Object.keys(object)) {
        const decoded = this.decode(object[key], values ?? field(fields, key));
        if (decoded !== object[key]) object[key] = decoded;
      }
    }
    return value;
  }

  /** Parses JSON, reading decimals at the places `shape` marks as strings. */
  parse(text: string, shape?: Shape): unknown {
    return this.decode(this.read(text), shape);
  }

  /**
   * Serializes a value: decimals given as strings are written as JSON numbers with their exact
   * digits, and dates as `YYYY-MM-DDTHH:mm:ss+0000`.
   *
   * @throws TypeError if a decimal string is not a number.
   */
  stringify(value: unknown, shape?: Shape): string {
    return JSON.stringify(this.#prepare(value, shape, ''), replaceDates);
  }

  /** Returns the shape of a field of a value of shape `shape`. */
  field(shape: Shape | undefined, name: string): Shape | undefined {
    return field(this.#fields(shape), name);
  }

  #prepare(value: unknown, shape: Shape | undefined, path: string): unknown {
    if (shape === undefined || value === null || typeof value !== 'object') {
      return shape === DECIMAL && typeof value === 'string' ? decimal(value, path) : value;
    }
    if (Array.isArray(value)) {
      const item = this.#items(shape, 'array');
      return item === undefined
        ? value
        : value.map((entry, index) => this.#prepare(entry, item, `${path}[${index}]`));
    }
    if (value instanceof Date || value instanceof Blob) return value;
    const values = this.#items(shape, 'map');
    const fields = this.#fields(shape);
    const copy: Record<string, unknown> = {};
    for (const [key, entry] of Object.entries(value)) {
      const entryShape = values ?? field(fields, key);
      copy[key] = this.#prepare(entry, entryShape, path ? `${path}.${key}` : key);
    }
    return copy;
  }

  #items(shape: Shape | undefined, kind: 'array' | 'map'): Shape | undefined {
    return Array.isArray(shape) && shape[0] === kind ? (shape[1] as Shape) : undefined;
  }

  #fields(shape: Shape | undefined): Readonly<Record<string, Shape>> | undefined {
    if (typeof shape === 'string')
      return Object.hasOwn(this.#shapes, shape) ? this.#shapes[shape] : undefined;
    if (shape === undefined || Array.isArray(shape)) return undefined;
    return shape as Readonly<Record<string, Shape>>;
  }
}

function field(
  fields: Readonly<Record<string, Shape>> | undefined,
  name: string,
): Shape | undefined {
  return fields && Object.hasOwn(fields, name) ? fields[name] : undefined;
}

function decimal(text: string, path: string): unknown {
  if (!JSON_NUMBER.test(text)) {
    throw new TypeError(`${path || 'The value'} must be a decimal number, got "${text}"`);
  }
  return rawJSON ? rawJSON(text) : Number(text);
}

function replaceDates(this: unknown, key: string, value: unknown): unknown {
  const original = (this as Record<string, unknown>)[key];
  return original instanceof Date ? formatDateTime(original) : value;
}

/**
 * Formats a date the way the web application sends timestamps: `YYYY-MM-DDTHH:mm:ss+0000`, in
 * UTC and with whole seconds.
 *
 * @throws TypeError for an invalid date.
 */
export function formatDateTime(date: Date): string {
  if (Number.isNaN(date.getTime())) throw new TypeError('Invalid Date');
  const pad = (value: number, length = 2) => String(value).padStart(length, '0');
  const day = `${pad(date.getUTCFullYear(), 4)}-${pad(date.getUTCMonth() + 1)}-${pad(date.getUTCDate())}`;
  const time = `${pad(date.getUTCHours())}:${pad(date.getUTCMinutes())}:${pad(date.getUTCSeconds())}`;
  return `${day}T${time}+0000`;
}

/** Text of a scalar sent in a query string, a form or a header. */
export function formatScalar(value: unknown): string {
  return value instanceof Date ? formatDateTime(value) : String(value);
}

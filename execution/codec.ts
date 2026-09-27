/** Base58 and little-endian integers. No key material. */

const ALPHABET = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz";

export function b58decode(value: string): Uint8Array {
  if (typeof value !== "string" || value.length < 32 || value.length > 44) {
    throw new Error("malformed_pubkey");
  }
  let zeros = 0;
  while (zeros < value.length && value[zeros] === "1") zeros += 1;
  if (zeros === value.length) return new Uint8Array(zeros);
  const bytes = [0];
  for (let i = zeros; i < value.length; i += 1) {
    const carry = ALPHABET.indexOf(value[i]);
    if (carry < 0) throw new Error("malformed_pubkey");
    for (let j = 0; j < bytes.length; j += 1) bytes[j] *= 58;
    bytes[0] += carry;
    let overflow = 0;
    for (let j = 0; j < bytes.length; j += 1) {
      bytes[j] += overflow;
      overflow = (bytes[j] / 256) | 0;
      bytes[j] %= 256;
    }
    while (overflow > 0) {
      bytes.push(overflow % 256);
      overflow = (overflow / 256) | 0;
    }
  }
  const out = new Uint8Array(zeros + bytes.length);
  for (let i = 0; i < bytes.length; i += 1) out[out.length - 1 - i] = bytes[i];
  return out;
}

export function b58encode(bytes: Uint8Array): string {
  let zeros = 0;
  while (zeros < bytes.length && bytes[zeros] === 0) zeros += 1;
  if (zeros === bytes.length) return "1".repeat(bytes.length);
  const start = zeros;
  const digits = [0];
  for (let i = start; i < bytes.length; i += 1) {
    let carry = bytes[i];
    for (let j = 0; j < digits.length; j += 1) {
      carry += digits[j] << 8;
      digits[j] = carry % 58;
      carry = (carry / 58) | 0;
    }
    while (carry > 0) {
      digits.push(carry % 58);
      carry = (carry / 58) | 0;
    }
  }
  let out = "1".repeat(start);
  for (let i = digits.length - 1; i >= 0; i -= 1) out += ALPHABET[digits[i]];
  return out;
}

export function pubkeyBytes(value: string): Uint8Array {
  const decoded = b58decode(value);
  if (decoded.length !== 32) throw new Error("malformed_pubkey");
  return decoded;
}

export function u64le(value: bigint): Uint8Array {
  if (value < 0n || value > 0xffffffffffffffffn) throw new Error("u64_range");
  const out = new Uint8Array(8);
  let rest = value;
  for (let i = 0; i < 8; i += 1) {
    out[i] = Number(rest & 0xffn);
    rest >>= 8n;
  }
  return out;
}

export function concatBytes(parts: Uint8Array[]): Uint8Array {
  const size = parts.reduce((sum, part) => sum + part.length, 0);
  const out = new Uint8Array(size);
  let offset = 0;
  for (const part of parts) {
    out.set(part, offset);
    offset += part.length;
  }
  return out;
}

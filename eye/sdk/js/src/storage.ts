export interface LumaStorageAdapter {
  getItem(key: string): string | null | Promise<string | null>;
  setItem(key: string, value: string): void | Promise<void>;
  removeItem(key: string): void | Promise<void>;
}

export interface MMKVLike {
  getString(key: string): string | undefined;
  set(key: string, value: string): void;
  delete?: (key: string) => void;
  remove?: (key: string) => void;
}

export interface AsyncStorageLike {
  getItem(key: string): Promise<string | null | undefined>;
  setItem(key: string, value: string): Promise<void>;
  removeItem(key: string): Promise<void>;
}

/**
 * Wraps a react-native-mmkv v2/v3 instance as a synchronous LumaStorageAdapter.
 */
export function createMMKVStorage(mmkv: MMKVLike): LumaStorageAdapter {
  return {
    getItem(key) {
      const value = mmkv.getString(key);
      return value === undefined ? null : value;
    },
    setItem(key, value) {
      mmkv.set(key, value);
    },
    removeItem(key) {
      if (typeof mmkv.delete === "function") {
        mmkv.delete(key);
      } else {
        mmkv.remove?.(key);
      }
    },
  };
}

/**
 * Wraps @react-native-async-storage/async-storage as an async LumaStorageAdapter.
 */
export function createAsyncStorage(asyncStorage: AsyncStorageLike): LumaStorageAdapter {
  return {
    async getItem(key) {
      const value = await asyncStorage.getItem(key);
      return value ?? null;
    },
    setItem: (key, value) => asyncStorage.setItem(key, value),
    removeItem: (key) => asyncStorage.removeItem(key),
  };
}

export function createMemoryStorage(): LumaStorageAdapter {
  const map = new Map<string, string>();
  return {
    getItem: (key) => (map.has(key) ? (map.get(key) as string) : null),
    setItem: (key, value) => {
      map.set(key, value);
    },
    removeItem: (key) => {
      map.delete(key);
    },
  };
}

export function getBrowserLocalStorage(): LumaStorageAdapter | undefined {
  return typeof localStorage !== "undefined" ? localStorage : undefined;
}

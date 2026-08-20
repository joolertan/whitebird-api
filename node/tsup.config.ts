import { defineConfig } from 'tsup';

export default defineConfig({
  entry: ['src/index.ts', 'src/otp.ts'],
  format: ['esm', 'cjs'],
  target: 'es2022',
  dts: true,
  sourcemap: true,
  clean: true,
});

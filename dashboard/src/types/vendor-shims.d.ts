// recharts' own type declarations (types/chart/generateCategoricalChart.d.ts)
// do `import type { DebouncedFunc } from 'lodash'` for exactly one internal
// ref-handle field (the type of a throttled mouse-move handler) that this
// dashboard's code never touches. This project has no `@types/lodash` --
// Task 4 is the first thing to actually import from "recharts" (it was an
// installed-but-unused dependency before), and `skipLibCheck` is off, so
// `tsc --noEmit` walks into that .d.ts and fails on the missing type the
// moment recharts is used.
//
// A bare `declare module "lodash";` (no body) types every import as `any`,
// which is enough for value imports but NOT for this one: recharts' import
// is `import type`, then instantiates `DebouncedFunc<...>` as a generic
// type, and `any` can't be instantiated like a generic type ("Cannot use
// namespace 'DebouncedFunc' as a type"). So this declares that one type
// for real, matching lodash's own upstream shape, rather than adding a new
// dependency or loosening `skipLibCheck` project-wide for every
// third-party .d.ts.
declare module "lodash" {
  export interface DebouncedFunc<T extends (...args: any[]) => any> {
    (...args: Parameters<T>): ReturnType<T> | undefined;
    cancel(): void;
    flush(): ReturnType<T> | undefined;
  }
}

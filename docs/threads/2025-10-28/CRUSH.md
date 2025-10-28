## Build/Lint/Test Commands

### Build
- `npm run build` - Builds the entire frontend
- `cargo build` - Builds Rust components
- `docker-compose build` - Builds Docker images

### Lint
- `npm run lint` - Runs linting on frontend code
- `cargo clippy` - Lints Rust code

### Test
- `npm test` - Runs all frontend tests
- `npm test:run` - Runs tests with exit code reporting
- `cargo test` - Runs Rust unit tests
- To run a single test:
  - JavaScript: `npm test -- -t 'test name'`
  - Rust: `cargo test test_name`

## Code Style Guidelines

### Imports
- Use path aliases (`@/*` for frontend, `@test/*` for tests)
- Organize imports alphabetically with standard library first

### Formatting
- TypeScript: `prettier` enforced through `npm run format`
- Rust: `rustfmt` enforced through `cargo fmt`

### Types
- Use explicit types for function arguments and return values
- Avoid `any` type in TypeScript

### Naming
- Use camelCase for variables and functions
- Use PascalCase for classes and interfaces
- Prefix test files with `*.test.ts` or `*.test.tsx`

### Testing
- Use `describe`/`it` blocks for tests
- Use `expect` for assertions

### Error Handling
- Use Result type in Rust
- Use try/catch in JavaScript
- Avoid panics in production code

### Other
- Use `.env` for environment variables
- Follow Tauri security best practices
- Use Vitest globals (`describe`, `it`, `expect`) in tests

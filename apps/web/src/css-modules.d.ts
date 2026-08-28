// CSS Modules are resolved by the bundler, not by TypeScript, so it needs to be
// told that importing one yields a class-name map rather than an error.
declare module "*.module.css" {
  const classes: Readonly<Record<string, string>>;
  export default classes;
}

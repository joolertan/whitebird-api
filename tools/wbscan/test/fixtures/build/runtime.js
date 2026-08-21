var n = e => {
  let t;
  return () => (e && (t = e(), e = undefined), t);
};
export { n };

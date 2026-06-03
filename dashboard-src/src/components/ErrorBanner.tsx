export function ErrorBanner({ errors }: { errors: string[] }) {
  if (!errors.length) return null;
  return (
    <section className="error-list" role="alert">
      {errors.map((message) => (
        <div key={message}>{message}</div>
      ))}
    </section>
  );
}

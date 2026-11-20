import React from "react";

interface ErrorBoundaryState {
  hasError: boolean;
  message: string;
}

export class ErrorBoundary extends React.Component<
  { children: React.ReactNode },
  ErrorBoundaryState
> {
  constructor(props: { children: React.ReactNode }) {
    super(props);
    this.state = { hasError: false, message: "" };
  }

  static getDerivedStateFromError(error: unknown): ErrorBoundaryState {
    return {
      hasError: true,
      message: error instanceof Error ? error.message : "An unexpected error occurred.",
    };
  }

  override render() {
    if (this.state.hasError) {
      return (
        <div
          role="alert"
          style={{
            minHeight: "100vh",
            display: "flex",
            flexDirection: "column",
            alignItems: "center",
            justifyContent: "center",
            padding: "32px",
            background: "var(--bg-canvas)",
            color: "var(--ink)",
            textAlign: "center",
            gap: "12px",
          }}
        >
          <h1 className="font-display" style={{ fontSize: "24px", fontWeight: 500 }}>
            Something went wrong
          </h1>
          <p style={{ fontSize: "13.5px", color: "var(--ink-2)", maxWidth: "460px" }}>
            {this.state.message}
          </p>
          <button
            type="button"
            className="btn-tactile solid"
            onClick={() => window.location.reload()}
          >
            Reload workspace
          </button>
        </div>
      );
    }
    return this.props.children;
  }
}

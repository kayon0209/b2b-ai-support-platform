import { useEffect, useState } from "react";
import { useNavigate } from "react-router-dom";
import { finishOperatorLogin } from "../lib/operatorAuth";

export function AuthCallback() {
  const navigate = useNavigate();
  const [error, setError] = useState<string | null>(null);
  useEffect(() => {
    let active = true;
    void finishOperatorLogin()
      .then((path) => { if (active) navigate(path, { replace: true }); })
      .catch((reason: unknown) => {
        if (active) setError(reason instanceof Error ? reason.message : String(reason));
      });
    return () => { active = false; };
  }, [navigate]);
  return <main className="auth-callback" role="status">
    {error ? <><h1>登录未完成</h1><p>{error}</p><a href="/admin/workbench">返回工作台</a></>
      : <p>正在完成企业身份验证…</p>}
  </main>;
}

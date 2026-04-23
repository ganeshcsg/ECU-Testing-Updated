import { BrowserRouter, NavLink, Route, Routes } from "react-router-dom";
import { Cpu, History } from "lucide-react";
import { clsx } from "clsx";
import GeneratePage from "./pages/GeneratePage";
import HistoryPage from "./pages/HistoryPage";

function NavItem({ to, icon: Icon, label }: { to: string; icon: React.ElementType; label: string }) {
  return (
    <NavLink
      to={to}
      end={to === "/"}
      className={({ isActive }) =>
        clsx(
          "flex items-center gap-1.5 px-3 py-1.5 rounded-lg text-sm font-medium transition-colors",
          isActive ? "bg-indigo-700 text-white" : "text-indigo-200 hover:bg-indigo-700/60 hover:text-white"
        )
      }
    >
      <Icon size={15} />
      {label}
    </NavLink>
  );
}

export default function App() {
  return (
    <BrowserRouter>
      <div className="min-h-screen flex flex-col">
        <header className="bg-indigo-900 shadow-md">
          <div className="max-w-6xl mx-auto px-5 py-3 flex items-center justify-between">
            <div className="flex items-center gap-3">
              <div className="bg-indigo-700 p-1.5 rounded-lg">
                <Cpu size={20} className="text-indigo-100" />
              </div>
              <div>
                <p className="text-white font-bold text-sm leading-tight">ECU Testing AI</p>
                <p className="text-indigo-300 text-xs leading-tight">Test Case &amp; CAPL Generator</p>
              </div>
            </div>
            <nav className="flex items-center gap-1">
              <NavItem to="/" icon={Cpu} label="Generate" />
              <NavItem to="/history" icon={History} label="History" />
            </nav>
          </div>
        </header>

        <main className="flex-1 max-w-6xl mx-auto w-full px-5 py-5">
          <Routes>
            <Route path="/" element={<GeneratePage />} />
            <Route path="/history" element={<HistoryPage />} />
          </Routes>
        </main>
      </div>
    </BrowserRouter>
  );
}

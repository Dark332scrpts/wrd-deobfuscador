import subprocess
import tempfile
import os
import re
import glob
from flask import Flask, request, jsonify
from flask_cors import CORS

app = Flask(__name__)
CORS(app)

# ============================================================
# CONFIGURAÇÃO — caminho do Lua 5.1 no Render
# ============================================================
LUA_BIN = "./lua5.1"  # Vamos compilar junto

# ============================================================
# AMBIENTE FAKE LUA (mock)
# ============================================================
MOCK_ENV = r"""
local _real_print = print
local _real_type = type
local _captured = {}

local function escape_str(s)
    return '"' .. s:gsub('\\', '\\\\'):gsub('"', '\\"'):gsub('\n', '\\n') .. '"'
end

print = function(...)
    local args = {...}
    local parts = {}
    for i, v in ipairs(args) do
        table.insert(parts, tostring(v))
    end
    table.insert(_captured, table.concat(parts, "\t"))
end

local function create_dummy(name)
    local d = {}
    local mt = {
        __index = function(_, k)
            table.insert(_captured, "ACCESS: " .. name .. "." .. tostring(k))
            return create_dummy(name .. "." .. tostring(k))
        end,
        __call = function(_, ...)
            local a = {...}
            local argstr = ""
            for i, v in ipairs(a) do
                if i > 1 then argstr = argstr .. ", " end
                argstr = argstr .. tostring(v)
            end
            table.insert(_captured, "CALL: " .. name .. "(" .. argstr .. ")")
            return create_dummy(name .. "_result")
        end,
        __tostring = function() return name end,
        __newindex = function(_, k, v)
            table.insert(_captured, "SET: " .. name .. "." .. tostring(k) .. " = " .. tostring(v))
        end
    }
    setmetatable(d, mt)
    return d
end

local MockEnv = {}
setmetatable(MockEnv, {
    __index = function(t, k)
        if _G[k] then return _G[k] end
        return create_dummy(tostring(k))
    end,
    __newindex = function(t, k, v)
        rawset(t, k, v)
    end
})

_G.game = create_dummy("game")
_G.workspace = create_dummy("workspace")
_G.shared = MockEnv
_G.getfenv = function() return MockEnv end
_G.getgenv = function() return MockEnv end
_G.loadstring = function(s)
    table.insert(_captured, "LOADSTRING: " .. tostring(#s) .. " bytes")
    if type(s) == "string" and #s > 10 then
        table.insert(_captured, "LS_CONTENT: " .. s:sub(1, 2000))
    end
    return function() end
end
_G.load = _G.loadstring
_G.newproxy = function(b) return newproxy(b) end
_G.task = create_dummy("task")
_G.task.wait = function() return 0.1 end
_G.wait = function() return 0.1 end
_G.spawn = function(f) pcall(f) end
_G.Delay = function(_, f) pcall(f) end

return MockEnv, _captured
"""

# ============================================================
# EXTRAI CONSTANTES DA TABELA DE STRINGS
# ============================================================
def extract_constants(code, var_name):
    pattern = rf'\blocal\s+{re.escape(var_name)}\s*=\s*\{{'
    match = re.search(pattern, code)
    if not match:
        return None
    
    start = match.start()
    brace_idx = code.find('{', start)
    if brace_idx == -1:
        return None
    
    # Encontra o fim da tabela
    depth = 0
    quote = None
    idx = brace_idx
    while idx < len(code):
        c = code[idx]
        if quote:
            if c == '\\': idx += 2; continue
            if c == quote: quote = None
            idx += 1; continue
        if c in ("'", '"'): quote = c
        elif c == '{': depth += 1
        elif c == '}':
            depth -= 1
            if depth == 0:
                table_content = code[brace_idx:idx+1]
                return table_content
        idx += 1
    return None

# ============================================================
# DEOBFUSCA UM SCRIPT
# ============================================================
def deobfuscate_script(script):
    # Encontra a variável da tabela de strings
    match = re.search(r'local\s+([a-zA-Z0-9_]+)=\{"', script)
    if not match:
        return {"error": "Não encontrei a tabela de strings (formato WRD esperado)"}
    
    var_name = match.group(1)
    
    # Extrai a tabela de constantes
    constants = extract_constants(script, var_name)
    
    # Encontra o ponto de injeção
    idx_ret = script.rfind("return(function")
    if idx_ret == -1:
        idx_ret = script.rfind("return (function")
    if idx_ret == -1:
        return {"error": "Formato não reconhecido — não é WRD ou está muito ofuscado"}
    
    # Constrói o script com mock env
    before = script[:idx_ret]
    after = script[idx_ret:]
    
    # Substitui getfenv pelo MockEnv
    after = re.sub(r'getfenv\s*\(\s*\)\s*or\s*_ENV', 'MockEnv', after)
    after = re.sub(r'getfenv\s+and\s+getfenv\(\)or\s+_ENV', 'MockEnv', after)
    
    full_script = MOCK_ENV + "\n" + before + "\n" + after + "\n"
    full_script += f"""
local result = ""
for _, v in ipairs(_captured) do
    result = result .. v .. "\\n"
end
print("===CAPTURED_START===")
print(result)
print("===CAPTURED_END===")
"""
    
    # Salva em arquivo temporário
    with tempfile.NamedTemporaryFile(mode='w', suffix='.lua', delete=False, encoding='utf-8') as f:
        f.write(full_script)
        temp_path = f.name
    
    try:
        # Executa com Lua 5.1
        result = subprocess.run(
            [LUA_BIN, temp_path],
            capture_output=True,
            text=True,
            timeout=25
        )
        
        stdout = result.stdout
        stderr = result.stderr
        
        # Extrai o captured
        trace = ""
        if "===CAPTURED_START===" in stdout:
            trace = stdout.split("===CAPTURED_START===")[1].split("===CAPTURED_END===")[0].strip()
        
        # Decodifica constantes
        const_str = ""
        if constants:
            # Tenta executar as constantes em Lua pra decodificar
            const_script = f"""
{constants}
local out = "local Constants = {{\\n"
for i, v in ipairs({var_name}) do
    if type(v) == "string" then
        local escaped = '"' .. v:gsub('\\\\', '\\\\\\\\'):gsub('"', '\\\\"'):gsub('\\n', '\\\\n') .. '"'
        out = out .. "  [" .. i .. "] = " .. escaped .. ",\\n"
    end
end
out = out .. "}"
print(out)
"""
            with tempfile.NamedTemporaryFile(mode='w', suffix='.lua', delete=False, encoding='utf-8') as f:
                f.write(const_script)
                const_path = f.name
            
            try:
                const_result = subprocess.run(
                    [LUA_BIN, const_path],
                    capture_output=True,
                    text=True,
                    timeout=10
                )
                if const_result.stdout.strip():
                    const_str = const_result.stdout.strip()
            except:
                pass
            finally:
                if os.path.exists(const_path):
                    os.remove(const_path)
        
        return {
            "success": True,
            "constants": const_str,
            "trace": trace[:50000],  # Limita tamanho
            "deobfuscated": "",
            "stderr": stderr[:5000] if stderr else ""
        }
        
    except subprocess.TimeoutExpired:
        return {"error": "Timeout (25s) — script muito pesado ou loop infinito"}
    except Exception as e:
        return {"error": str(e)}
    finally:
        if os.path.exists(temp_path):
            os.remove(temp_path)

# ============================================================
# ROTAS FLASK
# ============================================================
@app.route('/')
def home():
    return jsonify({"status": "WRD Deobfuscator API", "version": "2.0", "endpoints": ["POST /deobfuscate"]})

@app.route('/deobfuscate', methods=['POST'])
def deobfuscate():
    data = request.get_json()
    if not data or 'script' not in data:
        return jsonify({"error": "Envie um JSON com 'script'"}), 400
    
    script = data['script']
    if len(script) > 500000:
        return jsonify({"error": "Script muito grande (max 500KB)"}), 400
    
    result = deobfuscate_script(script)
    return jsonify(result)

@app.route('/health')
def health():
    return jsonify({"status": "ok"})

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=10000)

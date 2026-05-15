import subprocess
import tempfile
import os
import re
import sys
from flask import Flask, request, jsonify
from flask_cors import CORS

app = Flask(__name__)
CORS(app)

# ============================================================
# Tenta importar lupa (Lua 5.1 embutido no Python)
# ============================================================
try:
    import lupa
    from lupa import LuaRuntime
    lua = LuaRuntime(unpack_returned_tuples=True)
    USING_LUPA = True
    print("✅ Usando lupa (Lua 5.1 embutido)")
except ImportError:
    USING_LUPA = False
    print("⚠️ lupa não disponível, usando Lua externo")

# ============================================================
# AMBIENTE FAKE LUA (executado dentro do runtime)
# ============================================================
MOCK_ENV_SETUP = """
local _captured = {}

local function escape_str(s)
    if s == nil then return "nil" end
    return '"' .. tostring(s):gsub('\\\\', '\\\\\\\\'):gsub('"', '\\\\"'):gsub('\\n', '\\\\n') .. '"'
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
                local vtype = type(v)
                if vtype == "string" then
                    argstr = argstr .. '"' .. tostring(v):sub(1, 100) .. '"'
                elseif vtype == "function" then
                    argstr = argstr .. "function"
                elseif vtype == "table" then
                    argstr = argstr .. "table"
                elseif vtype == "nil" then
                    argstr = argstr .. "nil"
                else
                    argstr = argstr .. tostring(v)
                end
            end
            table.insert(_captured, "CALL: " .. name .. "(" .. argstr .. ")")
            
            -- Intercepta HttpGet pra capturar URLs
            if name == "game.HttpGet" or name == "game.HttpGetAsync" then
                local url = a[2] or (a[1] and type(a[1]) == "string" and a[1]) or ""
                table.insert(_captured, "URL_DETECTED: " .. tostring(url))
            end
            
            return create_dummy(name .. "_result")
        end,
        __tostring = function() return name end,
        __newindex = function(_, k, v)
            if type(v) == "string" and #v > 5 then
                table.insert(_captured, "SET: " .. name .. "." .. tostring(k) .. " = \"" .. tostring(v):sub(1, 200) .. "\"")
            else
                table.insert(_captured, "SET: " .. name .. "." .. tostring(k) .. " = " .. tostring(v))
            end
            rawset(d, k, v)
        end,
        __len = function() return 1 end,
        __add = function(a, b) return create_dummy("add") end,
        __sub = function(a, b) return create_dummy("sub") end,
        __mul = function(a, b) return create_dummy("mul") end,
        __div = function(a, b) return create_dummy("div") end,
        __eq = function(a, b) return false end,
        __lt = function(a, b) return false end,
        __le = function(a, b) return false end,
    }
    setmetatable(d, mt)
    return d
end

-- Mock do game
local game_mock = create_dummy("game")
game_mock.HttpGet = function(self, url)
    table.insert(_captured, "URL_DETECTED: " .. tostring(url))
    return ""
end
game_mock.HttpGetAsync = game_mock.HttpGet
game_mock.GetService = function(self, svc)
    return create_dummy("Service." .. tostring(svc))
end
game_mock.IsLoaded = function() return true end
game_mock.PlaceId = 123456
game_mock.GameId = 123456

-- Mock do workspace
local workspace_mock = create_dummy("workspace")

-- MockEnv
local MockEnv = {}
setmetatable(MockEnv, {
    __index = function(t, k)
        local kstr = tostring(k)
        if kstr == "game" then return game_mock end
        if kstr == "workspace" then return workspace_mock end
        if kstr == "shared" then return MockEnv end
        if _G[k] ~= nil then return _G[k] end
        return create_dummy(kstr)
    end,
    __newindex = function(t, k, v)
        rawset(t, k, v)
    end
})

-- Funções globais mockadas
MockEnv.game = game_mock
MockEnv.workspace = workspace_mock
MockEnv.shared = MockEnv
MockEnv.getfenv = function() return MockEnv end
MockEnv.getgenv = function() return MockEnv end
MockEnv.getrenv = function() return MockEnv end
MockEnv.getreg = function() return MockEnv end
MockEnv.loadstring = function(s, name)
    if type(s) == "string" then
        table.insert(_captured, "LOADSTRING: " .. tostring(#s) .. " bytes")
        if #s > 10 then
            table.insert(_captured, "LS_CONTENT_START")
            table.insert(_captured, tostring(s):sub(1, 5000))
            table.insert(_captured, "LS_CONTENT_END")
        end
    end
    return function() end
end
MockEnv.load = MockEnv.loadstring
MockEnv.task = create_dummy("task")
MockEnv.task.wait = function(s) return 0.1 end
MockEnv.task.spawn = function(f, ...) 
    local ok, err = pcall(f, ...)
    if not ok then table.insert(_captured, "SPAWN_ERROR: " .. tostring(err)) end
end
MockEnv.wait = function(s) return 0.1 end
MockEnv.spawn = MockEnv.task.spawn
MockEnv.Delay = function(t, f) pcall(f) end
MockEnv.print = function(...)
    local parts = {}
    for _, v in ipairs({...}) do
        table.insert(parts, tostring(v))
    end
    table.insert(_captured, table.concat(parts, "\\t"))
end
MockEnv.warn = MockEnv.print
MockEnv.error = MockEnv.print
MockEnv.require = function(m)
    table.insert(_captured, "REQUIRE: " .. tostring(m))
    return create_dummy(tostring(m))
end
MockEnv.newproxy = function(b)
    return create_dummy("newproxy")
end
MockEnv.setmetatable = setmetatable
MockEnv.getmetatable = getmetatable
MockEnv.pcall = function(f, ...)
    local args = {...}
    local ok, err = pcall(f, unpack(args))
    if not ok then
        table.insert(_captured, "PCALL_ERROR: " .. tostring(err))
        return false, tostring(err)
    end
    return ok, err
end
MockEnv.xpcall = xpcall
MockEnv.select = select
MockEnv.unpack = unpack
MockEnv.tostring = tostring
MockEnv.tonumber = tonumber
MockEnv.type = type
MockEnv.rawset = rawset
MockEnv.rawget = rawget
MockEnv.next = next
MockEnv.pairs = pairs
MockEnv.ipairs = ipairs
MockEnv.string = string
MockEnv.table = table
MockEnv.math = math
MockEnv.os = os
MockEnv.coroutine = coroutine

-- Expõe pra ser acessível
_G.MockEnv = MockEnv
_G._captured = _captured
_G.game = game_mock
_G.workspace = workspace_mock
_G.shared = MockEnv
_G.getfenv = function() return MockEnv end
_G.getgenv = function() return MockEnv end
_G.loadstring = MockEnv.loadstring
_G.load = MockEnv.load
_G.task = MockEnv.task
_G.wait = MockEnv.wait
_G.spawn = MockEnv.spawn
_G.Delay = MockEnv.Delay
_G.require = MockEnv.require
_G.newproxy = MockEnv.newproxy

print("MOCK_ENV_READY")
"""

# ============================================================
# EXTRAI CONSTANTES DA TABELA DE STRINGS (estático)
# ============================================================
def extract_constants_static(code):
    """Extrai a tabela de strings e decodifica os bytes"""
    # Encontra a tabela
    match = re.search(r'local\s+(\w+)\s*=\s*\{', code)
    if not match:
        return None
    
    var_name = match.group(1)
    start = match.start()
    brace_idx = code.find('{', start)
    
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
                table_str = code[brace_idx+1:idx]
                # Decodifica as strings com bytes escapados
                decoded = decode_byte_strings(table_str)
                return decoded
        idx += 1
    return None

def decode_byte_strings(table_str):
    """Decodifica strings com \\xxx\\xxx\\xxx"""
    strings = []
    pattern = r'"((?:\\\d{3})+)"'
    for match in re.finditer(pattern, table_str):
        byte_str = match.group(1)
        bytes_list = re.findall(r'\\(\d{3})', byte_str)
        decoded = ''.join(chr(int(b)) for b in bytes_list)
        # Filtra lixo
        printable = sum(1 for c in decoded if 32 <= ord(c) <= 126 or ord(c) > 160)
        if printable > len(decoded) * 0.3:
            strings.append(decoded)
    
    if not strings:
        return None
    
    result = "local Constants = {\n"
    for i, s in enumerate(strings):
        escaped = s.replace('\\', '\\\\').replace('"', '\\"').replace('\n', '\\n')
        result += f'  [{i+1}] = "{escaped}",\n'
    result += "}"
    return result

# ============================================================
# EXECUTA O SCRIPT COM LUPA (Lua 5.1 real)
# ============================================================
def execute_with_lupa(script):
    """Executa o script WRD com ambiente fake usando lupa"""
    if not USING_LUPA:
        return None, "lupa não instalado"
    
    try:
        # Configura o ambiente
        lua.execute(MOCK_ENV_SETUP)
        
        # Encontra a variável da tabela
        match = re.search(r'local\s+(\w+)\s*=\s*\{', script)
        if not match:
            return None, "Tabela de strings não encontrada"
        
        # Encontra ponto de injeção
        idx_ret = script.rfind("return(function")
        if idx_ret == -1:
            idx_ret = script.rfind("return (function")
        if idx_ret == -1:
            return None, "Formato não reconhecido"
        
        # Substitui getfenv pelo MockEnv
        before = script[:idx_ret]
        after = script[idx_ret:]
        after = re.sub(r'getfenv\s*\(\s*\)\s*or\s*_ENV', 'MockEnv', after)
        after = re.sub(r'getfenv\s+and\s+getfenv\(\)or\s+_ENV', 'MockEnv', after)
        after = re.sub(r'getfenv\s*\(\s*\)', 'MockEnv', after)
        
        full_script = before + "\n" + after
        
        # Executa com proteção
        wrapped = f"""
local ok, err = pcall(function()
    {full_script}
end)
if not ok then
    table.insert(_captured, "EXEC_ERROR: " .. tostring(err))
end
"""
        lua.execute(wrapped)
        
        # Coleta o captured
        captured = lua.eval('_captured')
        if captured:
            return '\n'.join(str(v) for v in captured.values()), None
        return "", None
        
    except Exception as e:
        return None, str(e)

# ============================================================
# FUNÇÃO PRINCIPAL DE DEOBFUSCAÇÃO
# ============================================================
def deobfuscate_script(script):
    result = {
        "success": True,
        "constants": "",
        "trace": "",
        "deobfuscated": "",
        "error": None
    }
    
    # 1. Extrai constantes (estático - sempre funciona)
    constants = extract_constants_static(script)
    if constants:
        result["constants"] = constants
    
    # 2. Executa com lupa (dinâmico)
    if USING_LUPA:
        trace, err = execute_with_lupa(script)
        if err:
            result["error"] = err
            result["success"] = False
        if trace:
            result["trace"] = trace[:50000]
    else:
        result["error"] = "Backend sem Lua (lupa não instalado)"
        result["success"] = False
    
    # 3. Tenta reconstruir código limpo a partir do trace
    if result["trace"] and "LS_CONTENT_START" in result["trace"]:
        # Extrai conteúdo de loadstring
        parts = result["trace"].split("LS_CONTENT_START")
        if len(parts) > 1:
            content = parts[1].split("LS_CONTENT_END")[0].strip()
            if len(content) > 50:
                # Tenta decodificar escapes
                try:
                    decoded = content.encode('utf-8').decode('unicode_escape')
                    result["deobfuscated"] = decoded
                except:
                    result["deobfuscated"] = content
    
    return result

# ============================================================
# ROTAS
# ============================================================
@app.route('/')
def home():
    return jsonify({
        "status": "online",
        "engine": "lupa" if USING_LUPA else "none",
        "endpoints": ["POST /deobfuscate", "GET /health"]
    })

@app.route('/deobfuscate', methods=['POST'])
def deobfuscate():
    data = request.get_json()
    if not data or 'script' not in data:
        return jsonify({"error": "Envie JSON com campo 'script'"}), 400
    
    script = data['script']
    if len(script) > 500000:
        return jsonify({"error": "Script muito grande (máx 500KB)"}), 400
    
    try:
        result = deobfuscate_script(script)
        return jsonify(result)
    except Exception as e:
        return jsonify({"error": str(e), "success": False}), 500

@app.route('/health')
def health():
    return jsonify({"status": "ok", "engine": "lupa" if USING_LUPA else "none"})

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 10000))
    app.run(host='0.0.0.0', port=port)

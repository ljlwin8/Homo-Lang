#ifndef HOMO_RUNTIME_HPP
#define HOMO_RUNTIME_HPP

// homo 语言运行时：一个会自动携带类型标签的值。
// 变量在 homo 里不写类型，赋值时由值本身决定类型。

#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <cerrno>
#include <cmath>
#include <charconv>
#include <memory>
#include <random>
#include <string>
#include <utility>
#include <initializer_list>
#include <vector>

struct homo_value {
    enum Kind { INT, DOUBLE, STRING, BOOL, NIL };

    Kind kind = INT;
    long long i = 0;
    double d = 0.0;
    std::string s;

    homo_value() = default;
    homo_value(int v) : kind(INT), i(v) {}
    homo_value(long v) : kind(INT), i((long long)v) {}
    homo_value(long long v) : kind(INT), i(v) {}
    homo_value(unsigned int v) : kind(INT), i((long long)v) {}
    homo_value(unsigned long v) : kind(INT), i((long long)v) {}
    homo_value(double v) : kind(DOUBLE), d(v) {}
    homo_value(float v) : kind(DOUBLE), d((double)v) {}
    homo_value(const char* v) : kind(STRING), s(v ? v : "") {}
    homo_value(std::string v) : kind(STRING), s(std::move(v)) {}
    homo_value(bool v) : kind(BOOL), i(v ? 1 : 0) {}

    long long as_int() const {
        switch (kind) {
            case DOUBLE: return (long long)d;
            case STRING: return std::atoll(s.c_str());
            default:     return i;
        }
    }

    double as_double() const {
        switch (kind) {
            case DOUBLE: return d;
            case STRING: return std::atof(s.c_str());
            default:     return (double)i;
        }
    }

    std::string as_string() const {
        switch (kind) {
            case NIL:    return "虚无";
            case STRING: return s;
            case DOUBLE: {
                // 最短往返表示：能还原同一个 double 的最少位数，去掉无意义的 0。
                // 整数倍的浮点保留「.0」，与整数区分；负零统一显示为 0.0。
                if (d == 0.0) return "0.0";
                char buf[64];
                auto res = std::to_chars(buf, buf + sizeof buf, d);
                if (res.ec != std::errc()) {
                    std::snprintf(buf, sizeof buf, "%.17g", d);
                    return std::string(buf);
                }
                std::string out(buf, res.ptr);
                if (std::isfinite(d) && out.find_first_of(".eE") == std::string::npos)
                    out += ".0";
                return out;
            }
            default: return std::to_string(i);
        }
    }

    bool truthy() const {
        switch (kind) {
            case NIL:    return false;
            case DOUBLE: return d != 0.0;
            case STRING: return !s.empty();
            default:     return i != 0;
        }
    }
};

inline bool homo_truthy(const homo_value& v) { return v.truthy(); }

// 虚无：空值。为假，打印为「虚无」，只与另一个虚无相等。
inline homo_value homo_null_value() {
    homo_value v;
    v.kind = homo_value::NIL;
    return v;
}

// 运行期错误统一从这里退出，错误信息带 homo 源码行号。
[[noreturn]] inline void homo_runtime_error(long long line, const std::string& msg) {
    std::fprintf(stderr, "运行错误（第 %lld 行）：%s\n", line, msg.c_str());
    std::exit(1);
}

// homo 的语言层面不分类型：变量、参数、返回值都可能装标量、数组或对象。
struct homo_any;

struct homo_object {
    virtual ~homo_object() = default;
    virtual homo_any call(const std::string& name, std::vector<homo_any> args, long long line);
    virtual homo_any& get_field(const std::string& name, long long line);
    virtual void set_field(const std::string& name, homo_any value, long long line);
    virtual std::string repr(int depth) const;
};

struct homo_any {
    homo_value scalar;
    std::vector<homo_any> array;   // 元素也可以是对象或数组
    std::shared_ptr<homo_object> obj;
    bool is_array = false;

    homo_any() = default;
    homo_any(const homo_value& v) : scalar(v) {}
    homo_any(homo_value&& v) : scalar(std::move(v)) {}
    homo_any(const std::vector<homo_any>& v) : array(v), is_array(true) {}
    homo_any(std::vector<homo_any>&& v) : array(std::move(v)), is_array(true) {}
    homo_any(std::shared_ptr<homo_object> o) : obj(std::move(o)) {}

    homo_any& operator=(const homo_value& v) {
        scalar = v;
        array.clear();
        obj.reset();
        is_array = false;
        return *this;
    }
    homo_any& operator=(const std::vector<homo_any>& v) {
        array = v;
        obj.reset();
        is_array = true;
        return *this;
    }
    homo_any& operator=(std::vector<homo_any>&& v) {
        array = std::move(v);
        obj.reset();
        is_array = true;
        return *this;
    }

    operator const homo_value&() const { return scalar; }

    long long as_int() const { return scalar.as_int(); }
    double as_double() const { return scalar.as_double(); }
    std::string as_string() const { return scalar.as_string(); }
    bool truthy() const { return scalar.truthy(); }
};

inline homo_any homo_null() { return homo_any(homo_null_value()); }

inline homo_any homo_object::call(const std::string& name, std::vector<homo_any>, long long line) {
    homo_runtime_error(line, "对象没有方法「" + name + "」");
}

inline homo_any& homo_object::get_field(const std::string& name, long long line) {
    homo_runtime_error(line, "对象没有字段「" + name + "」");
}

inline void homo_object::set_field(const std::string& name, homo_any, long long line) {
    homo_runtime_error(line, "对象没有字段「" + name + "」");
}

inline std::string homo_object::repr(int) const {
    return "{}";
}

// 把自定义类型的对象装进 homo_any：对象按引用共享，方法调用能改到原对象。
template <typename T>
inline homo_any homo_object_of(T v) {
    return homo_any(std::make_shared<T>(std::move(v)));
}

inline homo_any homo_call(const homo_any& o, const std::string& name,
                          std::vector<homo_any> args, long long line) {
    if (!o.obj) homo_runtime_error(line, "不能对非对象调用方法「" + name + "」");
    return o.obj->call(name, std::move(args), line);
}

// 新建对象后自动执行「初始化」；返回同一个对象，便于继续用后缀。
inline homo_any homo_init_object(homo_any o, const std::string& name,
                                 std::vector<homo_any> args, long long line) {
    homo_call(o, name, std::move(args), line);
    return o;
}

// 字段读写都通过对象转发；返回引用，字段里的数组元素也能就地修改。
inline homo_any& homo_get_field(const homo_any& o, const std::string& name, long long line) {
    if (!o.obj) homo_runtime_error(line, "不能对非对象读写字段「" + name + "」");
    return o.obj->get_field(name, line);
}

inline void homo_set_field(const homo_any& o, const std::string& name, homo_any value,
                           long long line) {
    if (!o.obj) homo_runtime_error(line, "不能对非对象读写字段「" + name + "」");
    o.obj->set_field(name, std::move(value), line);
}

// 数组：下标必须存在，越界直接终止程序。
inline std::vector<homo_any> homo_make_array(const homo_value& n) {
    long long k = n.as_int();
    if (k < 0) k = 0;
    return std::vector<homo_any>((std::size_t)k, homo_any(homo_value(0)));
}

inline homo_any& homo_index(std::vector<homo_any>& v, const homo_value& idx, long long line) {
    long long k = idx.as_int();
    if (k < 0 || k >= (long long)v.size()) {
        homo_runtime_error(line, "数组下标越界：下标 " + std::to_string(k) +
                                 "，长度 " + std::to_string(v.size()));
    }
    return v[(std::size_t)k];
}

inline homo_any& homo_index(homo_any& v, const homo_value& idx, long long line) {
    if (!v.is_array) {
        if (!v.obj && v.scalar.kind == homo_value::STRING)
            homo_runtime_error(line, "字符串不能按下标赋值");
        homo_runtime_error(line, "不能对非数组取下标");
    }
    return homo_index(v.array, idx, line);
}

// UTF-8：一个字符从非续字节开始；字符数按起始字节计。
inline std::size_t homo_utf8_len(const std::string& s) {
    std::size_t n = 0;
    for (unsigned char c : s)
        if ((c & 0xC0) != 0x80) ++n;
    return n;
}

// 第 i 个字符的字节起点与长度；越界返回 false。
inline bool homo_utf8_at(const std::string& s, long long i,
                         std::size_t& pos, std::size_t& len) {
    if (i < 0) return false;
    long long seen = 0;
    std::size_t start = std::string::npos;
    for (std::size_t k = 0; k <= s.size(); ++k) {
        bool boundary = k == s.size() || ((unsigned char)s[k] & 0xC0) != 0x80;
        if (!boundary) continue;
        if (start != std::string::npos && seen - 1 == i) {
            pos = start;
            len = k - start;
            return true;
        }
        start = k;
        ++seen;
    }
    return false;
}

// 按值读下标：数组取元素，字符串取字符；越界和类型不符直接终止。
inline homo_any homo_index_get(const homo_any& v, const homo_value& idx, long long line) {
    long long k = idx.as_int();
    if (v.is_array) {
        if (k < 0 || k >= (long long)v.array.size()) {
            homo_runtime_error(line, "数组下标越界：下标 " + std::to_string(k) +
                                     "，长度 " + std::to_string(v.array.size()));
        }
        return v.array[(std::size_t)k];
    }
    if (v.obj) homo_runtime_error(line, "不能对对象取下标");
    if (v.scalar.kind == homo_value::STRING) {
        std::size_t pos = 0, len = 0;
        if (!homo_utf8_at(v.scalar.s, k, pos, len)) {
            homo_runtime_error(line, "字符串下标越界：下标 " + std::to_string(k) +
                                     "，长度 " + std::to_string(homo_utf8_len(v.scalar.s)));
        }
        return homo_any(homo_value(v.scalar.s.substr(pos, len)));
    }
    homo_runtime_error(line, "不能对非数组或字符串取下标");
}

// 可变参数：调用点把多余的实参打包成数组。
inline std::vector<homo_any> homo_rest(const std::vector<homo_any>& args, std::size_t from) {
    if (from >= args.size()) return std::vector<homo_any>();
    return std::vector<homo_any>(args.begin() + (long)from, args.end());
}

// 长度：数组元素数，或字符串的字符数。
inline homo_value homo_length(const homo_any& v, long long line) {
    if (v.is_array) return homo_value((long long)v.array.size());
    if (!v.obj && v.scalar.kind == homo_value::STRING)
        return homo_value((long long)homo_utf8_len(v.scalar.s));
    homo_runtime_error(line, "只有数组和字符串有长度");
}

// 字符串内置函数。参数一律收 homo_any，避免数组被隐式退化成标量。

inline const std::string& homo_str_arg(const homo_any& v, const char* what, long long line) {
    if (v.is_array || v.obj || v.scalar.kind != homo_value::STRING)
        homo_runtime_error(line, std::string(what) + "必须是字符串");
    return v.scalar.s;
}

// 前 i 个字符的字节数；i 允许等于总字数。
inline bool homo_utf8_prefix_bytes(const std::string& s, long long i, std::size_t& out) {
    if (i < 0) return false;
    long long seen = 0;
    for (std::size_t k = 0; k <= s.size(); ++k) {
        if (k < s.size() && ((unsigned char)s[k] & 0xC0) == 0x80) continue;
        if (seen == i) {
            out = k;
            return true;
        }
        ++seen;
    }
    return false;
}

inline homo_value homo_substr_range(const std::string& s, long long start, long long len,
                                    long long line) {
    long long n = (long long)homo_utf8_len(s);
    if (start < 0 || start > n || len < 0 || len > n - start) {
        homo_runtime_error(line, "取子串越界：起点 " + std::to_string(start) +
                                 "，长度 " + std::to_string(len) +
                                 "，总长 " + std::to_string(n));
    }
    std::size_t b0 = 0, b1 = 0;
    homo_utf8_prefix_bytes(s, start, b0);
    homo_utf8_prefix_bytes(s, start + len, b1);
    return homo_value(s.substr(b0, b1 - b0));
}

inline homo_value homo_substr(const homo_any& s, const homo_value& start, long long line) {
    const std::string& str = homo_str_arg(s, "取子串 的第一个参数", line);
    long long n = (long long)homo_utf8_len(str);
    long long start_i = start.as_int();
    return homo_substr_range(str, start_i, start_i >= 0 && start_i <= n ? n - start_i : 0, line);
}

inline homo_value homo_substr_len(const homo_any& s, const homo_value& start,
                                  const homo_value& len, long long line) {
    const std::string& str = homo_str_arg(s, "取子串 的第一个参数", line);
    return homo_substr_range(str, start.as_int(), len.as_int(), line);
}

inline homo_value homo_find(const homo_any& s, const homo_any& sub, long long line) {
    const std::string& hay = homo_str_arg(s, "查找 的第一个参数", line);
    const std::string& needle = homo_str_arg(sub, "查找 的第二个参数", line);
    if (needle.empty()) return homo_value(0LL);
    std::size_t p = hay.find(needle);
    if (p == std::string::npos) return homo_value(-1LL);
    return homo_value((long long)homo_utf8_len(hay.substr(0, p)));
}

inline homo_value homo_contains(const homo_any& s, const homo_any& sub, long long line) {
    return homo_value(homo_find(s, sub, line).as_int() >= 0);
}

inline homo_value homo_replace(const homo_any& s, const homo_any& old,
                               const homo_any& neu, long long line) {
    const std::string& hay = homo_str_arg(s, "替换 的第一个参数", line);
    const std::string& what = homo_str_arg(old, "替换 的旧串", line);
    const std::string& with = homo_str_arg(neu, "替换 的新串", line);
    if (what.empty()) homo_runtime_error(line, "替换的旧串不能为空");
    std::string out;
    std::size_t pos = 0;
    while (true) {
        std::size_t p = hay.find(what, pos);
        if (p == std::string::npos) {
            out += hay.substr(pos);
            break;
        }
        out += hay.substr(pos, p - pos);
        out += with;
        pos = p + what.size();
    }
    return homo_value(out);
}

inline homo_any homo_split(const homo_any& s, const homo_any& sep, long long line) {
    const std::string& hay = homo_str_arg(s, "分割 的第一个参数", line);
    const std::string& cut = homo_str_arg(sep, "分割 的分隔符", line);
    if (cut.empty()) homo_runtime_error(line, "分割的分隔符不能为空");
    std::vector<homo_any> out;
    std::size_t pos = 0;
    while (true) {
        std::size_t p = hay.find(cut, pos);
        if (p == std::string::npos) {
            out.push_back(homo_any(homo_value(hay.substr(pos))));
            break;
        }
        out.push_back(homo_any(homo_value(hay.substr(pos, p - pos))));
        pos = p + cut.size();
    }
    return homo_any(std::move(out));
}

inline homo_value homo_join(const homo_any& arr, const homo_any& sep, long long line) {
    if (!arr.is_array) homo_runtime_error(line, "连接 的第一个参数必须是数组");
    if (sep.is_array || sep.obj) homo_runtime_error(line, "连接 的分隔符必须是标量");
    std::string out;
    for (std::size_t i = 0; i < arr.array.size(); ++i) {
        if (i) out += sep.scalar.as_string();
        const homo_any& el = arr.array[i];
        if (el.is_array || el.obj) homo_runtime_error(line, "连接 只能连接标量");
        out += el.scalar.as_string();
    }
    return homo_value(out);
}

inline bool homo_ascii_space(char c) {
    return c == ' ' || c == '\t' || c == '\r' || c == '\n' || c == '\v' || c == '\f';
}

inline homo_value homo_trim(const homo_any& s, long long line) {
    const std::string& str = homo_str_arg(s, "去空白 的第一个参数", line);
    std::size_t b = 0, e = str.size();
    while (b < e && homo_ascii_space(str[b])) ++b;
    while (e > b && homo_ascii_space(str[e - 1])) --e;
    return homo_value(str.substr(b, e - b));
}

inline homo_value homo_to_number(const homo_any& s, long long line) {
    const std::string& raw = homo_str_arg(s, "转数字 的第一个参数", line);
    std::size_t b = 0, e = raw.size();
    while (b < e && homo_ascii_space(raw[b])) ++b;
    while (e > b && homo_ascii_space(raw[e - 1])) --e;
    std::string text = raw.substr(b, e - b);
    const char* begin = text.c_str();
    char* end = nullptr;
    errno = 0;
    bool want_double = text.find_first_of(".eE") != std::string::npos;
    if (!want_double) {
        long long v = std::strtoll(begin, &end, 10);
        if (errno == ERANGE || end == begin || *end != '\0')
            homo_runtime_error(line, "不是合法的数字：「" + text + "」");
        return homo_value(v);
    }
    double v = std::strtod(begin, &end);
    if (errno == ERANGE || end == begin || *end != '\0' || !std::isfinite(v))
        homo_runtime_error(line, "不是合法的数字：「" + text + "」");
    return homo_value(v);
}

inline homo_value homo_upper(const homo_any& s, long long line) {
    std::string str = homo_str_arg(s, "转大写 的第一个参数", line);
    for (char& c : str)
        if (c >= 'a' && c <= 'z') c = (char)(c - 'a' + 'A');
    return homo_value(str);
}

inline homo_value homo_lower(const homo_any& s, long long line) {
    std::string str = homo_str_arg(s, "转小写 的第一个参数", line);
    for (char& c : str)
        if (c >= 'A' && c <= 'Z') c = (char)(c - 'A' + 'a');
    return homo_value(str);
}

// 结构化打印：数组和对象按内容展开，字符串在容器里带引号。
constexpr int HOMO_REPR_MAX_DEPTH = 16;

inline std::string homo_quote_string(const std::string& s) {
    std::string out = "\"";
    for (unsigned char c : s) {
        switch (c) {
            case '\\': out += "\\\\"; break;
            case '"':  out += "\\\""; break;
            case '\n': out += "\\n"; break;
            case '\r': out += "\\r"; break;
            case '\t': out += "\\t"; break;
            default:
                if (c < 0x20) {
                    char buf[8];
                    std::snprintf(buf, sizeof buf, "\\x%02x", c);
                    out += buf;
                } else {
                    out += (char)c;
                }
        }
    }
    out += '"';
    return out;
}

inline std::string homo_repr(const homo_any& v, bool quote_string, int depth) {
    if (depth > HOMO_REPR_MAX_DEPTH) return "…";
    if (v.is_array) {
        std::string out = "[";
        for (std::size_t i = 0; i < v.array.size(); ++i) {
            if (i) out += ", ";
            out += homo_repr(v.array[i], true, depth + 1);
        }
        out += "]";
        return out;
    }
    if (v.obj) return v.obj->repr(depth);
    if (v.scalar.kind == homo_value::STRING && quote_string)
        return homo_quote_string(v.scalar.s);
    return v.scalar.as_string();
}

// 数组字面量 [a, b, c]：按列出的元素建数组。
inline std::vector<homo_any> homo_array_lit(std::initializer_list<homo_any> items) {
    return std::vector<homo_any>(items);
}

// 把字面量按声明的长度整理：不足补 0，多出的截掉。
inline std::vector<homo_any> homo_array_size(std::vector<homo_any> v, const homo_value& n) {
    long long k = n.as_int();
    if (k < 0) k = 0;
    v.resize((std::size_t)k, homo_any(homo_value(0)));
    return v;
}

// 多维数组：按每一维的长度逐层整理，缺的补 0，多余的截掉。
inline homo_any homo_array_fit_at(homo_any v, const std::vector<long long>& dims,
                                  std::size_t depth) {
    long long k = dims[depth];
    if (k < 0) k = 0;
    if (!v.is_array) v = homo_any();
    v.array.resize((std::size_t)k, homo_any(homo_value(0)));
    v.is_array = true;
    if (depth + 1 < dims.size()) {
        for (auto& item : v.array) {
            item = homo_array_fit_at(std::move(item), dims, depth + 1);
        }
    }
    return v;
}

inline homo_any homo_array_fit(homo_any v, std::initializer_list<homo_value> sizes) {
    std::vector<long long> dims;
    dims.reserve(sizes.size());
    for (const auto& size : sizes) dims.push_back(size.as_int());
    if (dims.empty()) return v;
    return homo_array_fit_at(std::move(v), dims, 0);
}

// 控制台读入：读取一个十进制整数，遇到 EOF 返回 0。
inline homo_value homo_read_int() {
    long long v = 0;
    if (std::scanf("%lld", &v) != 1) return homo_value(0);
    return homo_value(v);
}

// 读入一整行文本（先跳过上一行残留的空白与换行）。
inline homo_value homo_read_line() {
    int ch;
    do {
        ch = std::getchar();
    } while (ch == ' ' || ch == '\t' || ch == '\r' || ch == '\n');
    std::string line;
    if (ch != EOF) {
        line.push_back((char)ch);
        while ((ch = std::getchar()) != EOF && ch != '\n') {
            line.push_back((char)ch);
        }
        if (!line.empty() && line.back() == '\r') line.pop_back();
    }
    return homo_value(line);
}

// 随机整数：返回 [0, bound) 之间的值。
inline homo_value homo_random(const homo_value& bound) {
    static std::mt19937_64 rng(std::random_device{}());
    long long n = bound.as_int();
    if (n <= 0) return homo_value(0);
    return homo_value((long long)(rng() % (unsigned long long)n));
}

inline homo_value homo_not(const homo_value& v) { return homo_value(!v.truthy()); }

inline bool homo_both_string(const homo_value& a, const homo_value& b) {
    return a.kind == homo_value::STRING && b.kind == homo_value::STRING;
}

inline homo_value homo_add(const homo_value& a, const homo_value& b) {
    if (a.kind == homo_value::STRING || b.kind == homo_value::STRING)
        return homo_value(a.as_string() + b.as_string());
    if (a.kind == homo_value::DOUBLE || b.kind == homo_value::DOUBLE)
        return homo_value(a.as_double() + b.as_double());
    return homo_value(a.as_int() + b.as_int());
}

inline homo_value homo_sub(const homo_value& a, const homo_value& b) {
    if (a.kind == homo_value::DOUBLE || b.kind == homo_value::DOUBLE)
        return homo_value(a.as_double() - b.as_double());
    return homo_value(a.as_int() - b.as_int());
}

inline homo_value homo_mul(const homo_value& a, const homo_value& b) {
    if (a.kind == homo_value::DOUBLE || b.kind == homo_value::DOUBLE)
        return homo_value(a.as_double() * b.as_double());
    return homo_value(a.as_int() * b.as_int());
}

inline homo_value homo_div(const homo_value& a, const homo_value& b, long long line) {
    if (a.kind == homo_value::DOUBLE || b.kind == homo_value::DOUBLE) {
        double x = a.as_double(), y = b.as_double();
        if (y == 0.0) homo_runtime_error(line, "除数不能为 0");
        return homo_value(x / y);
    }
    long long x = a.as_int(), y = b.as_int();
    if (y == 0) homo_runtime_error(line, "除数不能为 0");
    return homo_value(x / y);
}

inline homo_value homo_mod(const homo_value& a, const homo_value& b, long long line) {
    if (a.kind == homo_value::DOUBLE || b.kind == homo_value::DOUBLE) {
        double y = b.as_double();
        if (y == 0.0) homo_runtime_error(line, "除数不能为 0");
        return homo_value(std::fmod(a.as_double(), y));
    }
    long long y = b.as_int();
    if (y == 0) homo_runtime_error(line, "除数不能为 0");
    return homo_value(a.as_int() % y);
}

inline homo_value homo_lt(const homo_value& a, const homo_value& b) {
    if (homo_both_string(a, b)) return homo_value(a.s < b.s);
    return homo_value(a.as_double() < b.as_double());
}

inline homo_value homo_le(const homo_value& a, const homo_value& b) {
    if (homo_both_string(a, b)) return homo_value(a.s <= b.s);
    return homo_value(a.as_double() <= b.as_double());
}

inline homo_value homo_gt(const homo_value& a, const homo_value& b) {
    if (homo_both_string(a, b)) return homo_value(a.s > b.s);
    return homo_value(a.as_double() > b.as_double());
}

inline homo_value homo_eq(const homo_value& a, const homo_value& b) {
    if (a.kind == homo_value::NIL || b.kind == homo_value::NIL)
        return homo_value(a.kind == b.kind);
    if (homo_both_string(a, b)) return homo_value(a.s == b.s);
    return homo_value(a.as_double() == b.as_double());
}

inline homo_value homo_ne(const homo_value& a, const homo_value& b) {
    if (a.kind == homo_value::NIL || b.kind == homo_value::NIL)
        return homo_value(a.kind != b.kind);
    if (homo_both_string(a, b)) return homo_value(a.s != b.s);
    return homo_value(a.as_double() != b.as_double());
}

// 数组按内容深比较；对象按引用比较（同一个对象才相等）。
inline homo_value homo_eq_any(const homo_any& a, const homo_any& b) {
    if (a.is_array || b.is_array) {
        if (!a.is_array || !b.is_array) return homo_value(false);
        if (a.array.size() != b.array.size()) return homo_value(false);
        for (std::size_t i = 0; i < a.array.size(); ++i) {
            if (!homo_eq_any(a.array[i], b.array[i]).truthy()) return homo_value(false);
        }
        return homo_value(true);
    }
    if (a.obj || b.obj) {
        return homo_value(a.obj && b.obj && a.obj == b.obj);
    }
    return homo_eq(a.scalar, b.scalar);
}

inline homo_value homo_ne_any(const homo_any& a, const homo_any& b) {
    return homo_value(!homo_eq_any(a, b).truthy());
}

inline homo_value homo_lt_any(const homo_any& a, const homo_any& b, long long line) {
    if (a.is_array || b.is_array || a.obj || b.obj)
        homo_runtime_error(line, "数组或对象不能比较大小");
    return homo_lt(a.scalar, b.scalar);
}

// 依次把每段当文本输出，并在最后自动换行。
inline void homo_print_parts(std::initializer_list<homo_any> parts) {
    for (const auto& p : parts) std::fputs(homo_repr(p, false, 0).c_str(), stdout);
    std::fputc('\n', stdout);
    std::fflush(stdout);
}

// 把任意值当作文本输出，并自动换行；容器会自动结构化。
inline void homo_print_value(const homo_any& v) {
    std::fputs(homo_repr(v, false, 0).c_str(), stdout);
    std::fputc('\n', stdout);
    std::fflush(stdout);
}

// 按 C 风格格式串输出；每个转换符消耗一个 homo_value。
inline void homo_print(const char* fmt, std::initializer_list<homo_value> args) {
    auto it = args.begin();
    const char* p = fmt;
    while (*p) {
        if (*p != '%') {
            std::fputc((unsigned char)*p, stdout);
            ++p;
            continue;
        }
        if (p[1] == '%') {
            std::fputc('%', stdout);
            p += 2;
            continue;
        }
        std::string spec = "%";
        ++p;
        while (*p && !std::strchr("diouxXfFeEgGaAcs", *p)) {
            spec += *p;
            ++p;
        }
        if (!*p) {
            std::fputs(spec.c_str(), stdout);
            break;
        }
        char conv = *p++;
        if (it == args.end()) continue;
        homo_value v = *it++;
        switch (conv) {
            case 'd': case 'i': case 'o': case 'u': case 'x': case 'X': {
                std::string base;
                for (char ch : spec) {
                    if (ch != 'l' && ch != 'h' && ch != 'z' && ch != 'j' && ch != 't')
                        base += ch;
                }
                base += "lld";
                std::printf(base.c_str(), (long long)v.as_int());
                break;
            }
            case 'f': case 'F': case 'e': case 'E':
            case 'g': case 'G': case 'a': case 'A': {
                spec += conv;
                std::printf(spec.c_str(), v.as_double());
                break;
            }
            case 'c': {
                spec += 'c';
                std::printf(spec.c_str(), (int)v.as_int());
                break;
            }
            case 's': {
                spec += 's';
                std::printf(spec.c_str(), v.as_string().c_str());
                break;
            }
            default:
                break;
        }
    }
    std::fflush(stdout);
}

inline void homo_print(const char* fmt) { homo_print(fmt, {}); }

#endif // HOMO_RUNTIME_HPP

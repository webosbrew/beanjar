// SPDX-FileCopyrightText: Mariotaku <git@mariotaku.me> and the Beanjar contributors
// SPDX-License-Identifier: GPL-2.0-or-later
/* lls-probe: the default records lowlevelstorage (LLS) writes to a new EEPROM, from LLS's own code.
 *
 *   lls-probe [LLS [debugstatus N [string|bool DB ITEM VALUE]...]]   (LLS: default
 * /usr/sbin/lowlevelstorage) lls-probe group
 *
 * Run as a 32-bit ARM program in the firmware's root (host/core/beanjar/board/lls.py: qemu-arm and
 * bwrap, with the board's /proc/cmdline and the tvconfig JSONs in /tmp/var/run/tvconfig). It loads
 * the LLS executable (dlopen; it NEEDs the five DB plugins) and calls what LLS's
 * LLSController::initPlugInDB calls for a DB whose valid mark does not match, through the same
 * lls_db_interface functions: getAllDBInfo (LLSDBMap's table: name, offset, size and flag of each
 * DB region), and per DB getDBSize, getInitData into a zeroed buffer of that size (initPlugInDB
 * zero-fills a new[] buffer), getValidMarkInfo (where the mark is in the record) and
 * getValidMarkMagic. One line per DB on stdout:
 *
 *   db NAME OFFSET REGION_SIZE FLAG SIZE MARK_OFFSET MARK_SIZE RECORD_HEX MAGIC_HEX
 *
 * Nothing is computed here: the caller places the bytes as initPlugInDB writes them.
 *
 * "group" asks the board's libdile_boardinfo instead (DILE_BOARDINFO_Initialze, then
 * DILE_BOARDINFO_GetCountryGroupCode into a byte): its country group of the command line, which
 * is countryGrp= where the line has one, else what it derives (W20L: model_id.json's region; W21P:
 * libdile_tvregion's region of hwopt='s area option or tvregion=). One line: "group N RESULT".
 *
 * The functions take std::string by value: the Itanium C++ ABI passes such a class as a pointer to
 * a temporary the caller owns. The temporaries are built here as libstdc++'s (the C++11 ABI's)
 * 24-byte strings (the local buffer, or the heap with the capacity in its place), and never freed.
 * Built with the webOS SDK's C compiler (C only: the probe uses no C++ runtime of its own). */
#define _GNU_SOURCE
#include <dlfcn.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>

/* std::__cxx11::basic_string<char> on 32-bit ARM */
struct cxxstr {
    char *p;
    uint32_t len;
    char local[16];
};

/* LLS_DB_INFO_T, an element of the vector getAllDBInfo fills: 36 bytes (initPlugInDB steps its
 * vector by 36; the name, then the region's offset, size and flag) */
struct db_info {
    struct cxxstr name;
    uint32_t offset, size, flag;
};

/* std::vector<LLS_DB_INFO_T> */
struct vec {
    struct db_info *begin, *end, *cap;
};

/* LLS_ITEM_INFO_T as getValidMarkInfo fills it: two strings, then the item's offset and size in
 * the record and one more word (initPlugInDB reads the offset at +48 and the size at +52) */
struct item_info {
    struct cxxstr name, kind;
    uint32_t offset, size, extra;
};

static void str_init(struct cxxstr *s, const char *text)
{
    size_t n = strlen(text);

    memset(s, 0, sizeof *s);
    s->len = n;
    if (n < sizeof s->local) {
        s->p = s->local;
    } else {
        /* a longer one on the heap, its capacity where the local buffer was */
        uint32_t cap = n;
        s->p = malloc(n + 1);
        memcpy(s->local, &cap, sizeof cap);
    }
    memcpy(s->p, text, n + 1);
}

static void *sym(void *h, const char *name)
{
    void *f = dlsym(h, name);

    if (!f) {
        fprintf(stderr, "lls-probe: %s: %s\n", name, dlerror());
        exit(1);
    }
    return f;
}

static void hex(const uint8_t *b, uint32_t n)
{
    for (uint32_t i = 0; i < n; i++)
        printf("%02x", b[i]);
    if (!n)
        printf("-");
}

static int group(void)
{
    void *h = dlopen("libdile_boardinfo.so.1", RTLD_NOW | RTLD_GLOBAL);
    uint8_t g = 0;

    if (!h) {
        /* Queue's stock C facade accepts no initialize arguments and a byte
         * output pointer for get_country_group_code (module stores with strb). */
        h = dlopen("libnyx.so.7", RTLD_NOW | RTLD_GLOBAL);
        if (!h) {
            fprintf(stderr, "lls-probe: board info: %s\n", dlerror());
            return 1;
        }
        int (*init)(void) = sym(h, "nyx_device_info_initialize");
        int (*get)(uint8_t *) = sym(h, "nyx_device_info_get_country_group_code");
        int r = init();
        if (!r)
            r = get(&g);
        printf("group %u %d\n", g, r);
        return r ? 1 : 0;
    }
    int (*init)(void) = sym(h, "DILE_BOARDINFO_Initialze");
    int (*get)(uint8_t *) = sym(h, "DILE_BOARDINFO_GetCountryGroupCode");
    init();
    int r = get(&g);
    printf("group %u %d\n", g, r);
    return 0;
}

/* Queue's NYX metadata has the same ARM field positions as the legacy
 * structs above: stock vector insertion strides36/60 and stock storage
 * reads DbInfo+24/28/32, ItemInfo+48/52 (W22H queue module/service).
 * Calls use exported methods directly, never a guessed vtable. */
static int nyx_probe(void *h, int argc, char **argv)
{
    void *(*create)(void) = sym(h, "nyx_create_LlsDb_object");
    void *obj = create();
    int (*all)(void *, struct vec *) =
        sym(h, "_ZN3nyx9LlsDbImpl12getAllDbInfoEPSt6vectorINS_9LlsDb_1_06DbInfoESaIS3_EE");
    int (*size_of)(void *, struct cxxstr *, uint32_t *) = sym(
        h, "_ZN3nyx9LlsDbImpl9getDbSizeENSt7__cxx1112basic_stringIcSt11char_traitsIcESaIcEEEPj");
    int (*init)(void *, struct cxxstr *, uint32_t, uint8_t *) = sym(
        h,
        "_ZN3nyx9LlsDbImpl11getInitDataENSt7__cxx1112basic_stringIcSt11char_traitsIcESaIcEEEjPh");
    int (*mark)(void *, struct cxxstr *, struct item_info *) =
        sym(h, "_ZN3nyx9LlsDbImpl16getValidMarkInfoENSt7__cxx1112basic_stringIcSt11char_"
               "traitsIcESaIcEEEPNS_9LlsDb_1_08ItemInfoE");
    int (*magic)(void *, struct cxxstr *, uint32_t, uint8_t *) =
        sym(h, "_ZN3nyx9LlsDbImpl17getValidMarkMagicENSt7__cxx1112basic_stringIcSt11char_"
               "traitsIcESaIcEEEjPh");
    int (*item)(void *, struct cxxstr *, struct cxxstr *, struct item_info *) =
        sym(h, "_ZN3nyx9LlsDbImpl11getItemInfoENSt7__cxx1112basic_stringIcSt11char_"
               "traitsIcESaIcEEES6_PNS_9LlsDb_1_08ItemInfoE");
    int (*convert)(void *, struct cxxstr *, struct cxxstr *, const void *, uint32_t, uint8_t *) =
        sym(h, "_ZN3nyx9LlsDbImpl19convertJsonToBinaryENSt7__cxx1112basic_stringIcSt11char_"
               "traitsIcESaIcEEES6_RKNS_9LlsDb_1_012ItemJsonDataEjPh");
    struct vec v = {0};
    int r = obj ? all(obj, &v) : -1;
    if (r || !v.begin || v.end <= v.begin || v.end - v.begin > 64) {
        fprintf(stderr, "lls-probe: NYX getAllDbInfo: %d, invalid DB list\n", r);
        return 1;
    }
    for (struct db_info *d = v.begin; d < v.end; ++d) {
        struct cxxstr name;
        struct item_info info;
        uint32_t n = 0;
        if (!d->name.len || d->name.len >= 64)
            return 1;
        str_init(&name, d->name.p);
        if ((r = size_of(obj, &name, &n)) || !n || n > 32768)
            return 1;
        uint8_t *rec = calloc(1, n);
        str_init(&name, d->name.p);
        if ((r = init(obj, &name, n, rec)))
            return 1;
        memset(&info, 0, sizeof info);
        str_init(&info.name, "");
        str_init(&info.kind, "");
        str_init(&name, d->name.p);
        if ((r = mark(obj, &name, &info)) || !info.size || info.size > n)
            return 1;
        uint8_t *m = calloc(1, info.size);
        str_init(&name, d->name.p);
        if ((r = magic(obj, &name, info.size, m)))
            return 1;
        printf("db %s %u %u %u %u %u %u ", d->name.p, d->offset, d->size, d->flag, n, info.offset,
               info.size);
        hex(rec, n);
        printf(" ");
        hex(m, info.size);
        printf("\n");
    }
    /* Convert debugstatus through the stock public API, so its actual system
     * object and crypto initialization remain owned by the module. */
    for (int a = 0; a < argc;) {
        const char *db, *key, *value, *kind = "string";
        if (a == 0) {
            if (argc < 4 || strcmp(argv[2], "debugstatus"))
                break;
            int mode = atoi(argv[3]);
            value = mode == 3 ? "DEBUG" : mode == 4 ? "EVENT" : mode == 5 ? "RELEASE" : NULL;
            if (!value)
                return 1;
            db = "system";
            key = "debugstatus";
            a = 4;
        } else {
            if (a + 3 >= argc || (strcmp(argv[a], "string") && strcmp(argv[a], "bool")))
                return 1;
            kind = argv[a];
            db = argv[a + 1];
            key = argv[a + 2];
            value = argv[a + 3];
            a += 4;
        }
        struct cxxstr d, k;
        struct item_info info;
        memset(&info, 0, sizeof info);
        str_init(&info.name, "");
        str_init(&info.kind, "");
        str_init(&d, db);
        str_init(&k, key);
        if ((r = item(obj, &d, &k, &info)) || !info.size || info.size > 32768) {
            fprintf(stderr, "lls-probe: NYX getItemInfo(%s,%s): %d\n", db, key, r);
            return 1;
        }
        /* The selected string and numeric-string callbacks read type0 and
         * std::string+4 only: queue stringGetSetter0x2ebb0, binaryGetSetter
         *0x2de74/type0 branch0x303a4, system debug0x5cb94. This is their
         * verified28-byte input prefix, not a declaration of the full type. */
        struct {
            uint32_t type;
            struct cxxstr text;
            int32_t number;
            uint8_t boolean;
        } json = {0};
        str_init(&json.text, value);
        if (!strcmp(kind, "bool")) { /* the same layout as LLS's item data, below */
            json.type = 2;
            json.boolean = !strcmp(value, "true");
        }
        uint8_t *buf = calloc(1, info.size);
        str_init(&d, db);
        str_init(&k, key);
        if ((r = convert(obj, &d, &k, &json, info.size, buf))) {
            fprintf(stderr, "lls-probe: NYX convert(%s,%s): %d\n", db, key, r);
            return 1;
        }
        printf("item %s %s %u %u ", db, key, info.offset, info.size);
        hex(buf, info.size);
        printf("\n");
        if (a == argc)
            break;
    }
    fflush(stdout);
    _exit(0);
}

static int probe_main(int argc, char **argv)
{
    if (argc == 2 && !strcmp(argv[1], "group"))
        return group();
    const char *lls = argc > 1 ? argv[1] : "/usr/sbin/lowlevelstorage";
    void *h = dlopen(
#ifdef LLS_PRELOAD
        NULL,
#else
        lls,
#endif
        RTLD_NOW | RTLD_GLOBAL);

    if (!h) {
        fprintf(stderr, "lls-probe: dlopen %s: %s\n", lls, dlerror());
        return 1;
    }
    if (dlsym(h, "nyx_create_LlsDb_object"))
        return nyx_probe(h, argc, argv);
    int (*all)(struct vec *) =
        sym(h, "_ZN16lls_db_interface12getAllDBInfoEPSt6vectorI13LLS_DB_INFO_TSaIS1_EE");
    int (*db_size)(struct cxxstr *, uint32_t *) = sym(
        h,
        "_ZN16lls_db_interface9getDBSizeENSt7__cxx1112basic_stringIcSt11char_traitsIcESaIcEEEPj");
    int (*init_data)(struct cxxstr *, uint32_t, uint8_t *) =
        sym(h, "_ZN16lls_db_interface11getInitDataENSt7__cxx1112basic_stringIcSt11char_"
               "traitsIcESaIcEEEjPh");
    int (*mark_info)(struct cxxstr *, struct item_info *) =
        sym(h, "_ZN16lls_db_interface16getValidMarkInfoENSt7__cxx1112basic_stringIcSt11char_"
               "traitsIcESaIcEEEP15LLS_ITEM_INFO_T");
    int (*mark_magic)(struct cxxstr *, uint32_t, uint8_t *) =
        sym(h, "_ZN16lls_db_interface17getValidMarkMagicENSt7__cxx1112basic_stringIcSt11char_"
               "traitsIcESaIcEEEjPh");

    struct vec v = {0};
    int r = all(&v);
    if (r || !v.begin || v.end <= v.begin) {
        fprintf(stderr, "lls-probe: getAllDBInfo: %d, no DBs\n", r);
        return 1;
    }
    for (struct db_info *d = v.begin; d < v.end; d++) {
        char name[16];
        struct cxxstr s;
        uint32_t size = 0;

        if (d->name.len >= sizeof name) {
            fprintf(stderr, "lls-probe: a DB name of %u bytes\n", d->name.len);
            return 1;
        }
        memcpy(name, d->name.p, d->name.len);
        name[d->name.len] = 0;
        str_init(&s, name);
        if ((r = db_size(&s, &size)) || !size) {
            fprintf(stderr, "lls-probe: %s: getDBSize %d, size %u\n", name, r, size);
            return 1;
        }
        uint8_t *rec = calloc(1, size);
        str_init(&s, name);
        if ((r = init_data(&s, size, rec))) {
            fprintf(stderr, "lls-probe: %s: getInitData %d\n", name, r);
            return 1;
        }
        struct item_info info;
        memset(&info, 0, sizeof info);
        str_init(&info.name, "");
        str_init(&info.kind, "");
        str_init(&s, name);
        if ((r = mark_info(&s, &info))) {
            fprintf(stderr, "lls-probe: %s: getValidMarkInfo %d\n", name, r);
            return 1;
        }
        uint8_t *magic = calloc(1, info.size + 1);
        str_init(&s, name);
        if ((r = mark_magic(&s, info.size, magic))) {
            fprintf(stderr, "lls-probe: %s: getValidMarkMagic %d\n", name, r);
            return 1;
        }
        printf("db %s %u %u %u %u %u %u ", name, d->offset, d->size, d->flag, size, info.offset,
               info.size);
        hex(rec, size);
        printf(" ");
        hex(magic, info.size);
        printf("\n");
    }
    /* lls-probe LLS debugstatus N: the system DB's debugstatus item as LLS stores a status N
     * (the board's debugMode=): where getItemInfo puts it, and the bytes of
     * LLSSystemDB::encryptDebugstatus(N), which convertJsonToDataDebugstatus calls with the number
     * of the name it is given (3 DEBUG, 4 EVENT, 5 RELEASE). One line: item system debugstatus
     * OFFSET SIZE HEX */
    if (argc > 3 && !strcmp(argv[2], "debugstatus")) {
        int (*item_info)(struct cxxstr *, struct cxxstr *, struct item_info *) =
            sym(h, "_ZN16lls_db_interface11getItemInfoENSt7__cxx1112basic_stringIcSt11char_"
                   "traitsIcESaIcEEES5_P15LLS_ITEM_INFO_T");
        void (*ctor)(void *) = sym(h, "_ZN11LLSSystemDBC1Ev");
        int (*encrypt)(void *, unsigned char, uint32_t, uint8_t *) =
            sym(h, "_ZNK11LLSSystemDB18encryptDebugstatusEhjPh");
        struct cxxstr db, item;
        struct item_info info;
        memset(&info, 0, sizeof info);
        str_init(&info.name, "");
        str_init(&info.kind, "");
        str_init(&db, "system");
        str_init(&item, "debugstatus");
        if ((r = item_info(&db, &item, &info)) || !info.size) {
            fprintf(stderr, "lls-probe: getItemInfo(system, debugstatus): %d, size %u\n", r,
                    info.size);
            return 1;
        }
        void *obj = calloc(1, 4096);
        ctor(obj);
        uint8_t *buf = calloc(1, info.size);
        if ((r = encrypt(obj, (unsigned char)atoi(argv[3]), info.size, buf))) {
            fprintf(stderr, "lls-probe: encryptDebugstatus(%s): %d\n", argv[3], r);
            return 1;
        }
        printf("item system debugstatus %u %u ", info.offset, info.size);
        hex(buf, info.size);
        printf("\n");
    }
    /* ... string DB ITEM VALUE: a string item as LLS's setData stores it, the bytes of
     * lls_db_interface::convertJsonToBinary(DB, ITEM, a string JSON value) at getItemInfo's place
     * (LLS_ITEM_JSON_DATA_T: the type at +0, 0 a string, which convertJsonToDataDebugstatus
     * tests, and the std::string at +4). bool DB ITEM true|false: a JSON boolean the same way
     * (type 2, the bool at +32; a number is type 1, the int at +28: W21P's
     * LLSService::getItemDataFromJsonDB, 0x1fe52-0x1ff5e). One line each: item DB ITEM OFFSET SIZE
     * HEX */
    for (int a = 4; a + 3 < argc && (!strcmp(argv[a], "string") || !strcmp(argv[a], "bool"));
         a += 4) {
        int (*item_info)(struct cxxstr *, struct cxxstr *, struct item_info *) =
            sym(h, "_ZN16lls_db_interface11getItemInfoENSt7__cxx1112basic_stringIcSt11char_"
                   "traitsIcESaIcEEES5_P15LLS_ITEM_INFO_T");
        int (*convert)(struct cxxstr *, struct cxxstr *, const void *, uint32_t, uint8_t *) =
            sym(h, "_ZN16lls_db_interface19convertJsonToBinaryENSt7__cxx1112basic_stringIcSt11char_"
                   "traitsIcESaIcEEES5_RK20LLS_ITEM_JSON_DATA_TjPh");
        struct cxxstr db, item;
        struct item_info info;
        memset(&info, 0, sizeof info);
        str_init(&info.name, "");
        str_init(&info.kind, "");
        str_init(&db, argv[a + 1]);
        str_init(&item, argv[a + 2]);
        if ((r = item_info(&db, &item, &info)) || !info.size) {
            fprintf(stderr, "lls-probe: getItemInfo(%s, %s): %d, size %u\n", argv[a + 1],
                    argv[a + 2], r, info.size);
            return 1;
        }
        uint8_t *json = calloc(1, 256); /* larger than LLS_ITEM_JSON_DATA_T */
        str_init((struct cxxstr *)(json + 4), argv[a + 3]);
        if (!strcmp(argv[a], "bool")) {
            *(uint32_t *)json = 2;
            json[32] = !strcmp(argv[a + 3], "true");
        }
        uint8_t *buf = calloc(1, info.size);
        str_init(&db, argv[a + 1]);
        str_init(&item, argv[a + 2]);
        if ((r = convert(&db, &item, json, info.size, buf))) {
            fprintf(stderr, "lls-probe: convertJsonToBinary(%s, %s, %s): %d\n", argv[a + 1],
                    argv[a + 2], argv[a + 3], r);
            return 1;
        }
        printf("item %s %s %u %u ", argv[a + 1], argv[a + 2], info.offset, info.size);
        hex(buf, info.size);
        printf("\n");
    }
    /* No exit handlers: the plugins' static destructors free shared std::map nodes twice when a
     * process with LLS loaded exits (LLS never does); the records are out. */
    fflush(stdout);
    _exit(0);
}

#ifdef LLS_PRELOAD
/* The stock PIE must be loaded as the executable. Replace only its main entry:
 * libc still performs the normal executable/library initialization first. */
static int preload_main(int argc, char **argv, char **envp)
{
    (void)envp;
    return probe_main(argc, argv);
}

int __libc_start_main(int (*main_fn)(int, char **, char **), int argc, char **argv,
                      void (*init_fn)(void), void (*fini_fn)(void), void (*rtld_fini)(void),
                      void *stack_end)
{
    typedef int (*start_fn)(int (*)(int, char **, char **), int, char **, void (*)(void),
                            void (*)(void), void (*)(void), void *);
    start_fn start = sym(RTLD_NEXT, "__libc_start_main");
    (void)main_fn;
    return start(preload_main, argc, argv, init_fn, fini_fn, rtld_fini, stack_end);
}
#else
int main(int argc, char **argv)
{
    return probe_main(argc, argv);
}
#endif

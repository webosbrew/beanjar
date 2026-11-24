// SPDX-FileCopyrightText: Mariotaku <git@mariotaku.me> and the Beanjar contributors
// SPDX-License-Identifier: GPL-2.0-or-later

// textshot-serve keeps go-textshot's models loaded and reads screen elements for
// `beanjar elements` and the viewer's Elements layer (host/core/README.md, "Reading
// screen elements"). It answers on a unix socket, one JSON object per line:
//
//	{"image": "/abs/frame.png"}  ->  {"elements": [...]} or {"error": "..."}
//	{"ping": true}               ->  {"ok": true, "textshot": "v0.2.0"}
//
// The elements array is the upstream textshot CLI's -json output for that image, made by
// the same code (jsonElement below, cmd/textshot's) from the same engine options, so the
// two are interchangeable. Readings are one at a time. It exits when no request came for
// -idle, removing its socket. The ONNX runtime is upstream's choice: the cached CPU
// download, or TEXTSHOT_ONNXRUNTIME_LIB / -lib (a CUDA build puts the element detector
// on the GPU, with upstream's own fallback to the CPU).
package main

import (
	"bufio"
	"context"
	"encoding/json"
	"flag"
	"fmt"
	"image"
	"image/color"
	_ "image/gif"
	_ "image/jpeg"
	_ "image/png"
	"log/slog"
	"net"
	"os"
	"os/signal"
	"runtime/debug"
	"strconv"
	"sync"
	"syscall"
	"time"

	"github.com/devcell-sh/go-textshot"
)

func main() {
	fs := flag.NewFlagSet("textshot-serve", flag.ExitOnError)
	socket := fs.String("socket", "", "the unix socket to answer on (required)")
	idle := fs.Duration("idle", 5*time.Minute, "exit after this long without a request")
	// The textshot CLI's engine options, with its defaults (cmd/textshot/main.go).
	cache := fs.String("cache", "", "cache directory (default $TEXTSHOT_CACHE_DIR or "+textshot.DefaultCacheDir()+")")
	lib := fs.String("lib", "", "libonnxruntime file or directory (default $TEXTSHOT_ONNXRUNTIME_LIB or cached download)")
	maxSide := fs.Int("max-side", 1920, "downscale images for OCR so the longer side is at most this many px")
	elemMaxSide := fs.Int("element-max-side", 1280, "downscale images for element detection so the longer side is at most this many px")
	threads := fs.Int("threads", 4, "intra-op inference threads")
	minScore := fs.Float64("min-score", 0.5, "drop text lines with confidence below this (-1 keeps all)")
	elemMinScore := fs.Float64("element-min-score", 0.1, "drop elements with confidence below this")
	verbose := fs.Bool("v", false, "debug logging")
	fs.Parse(os.Args[1:])
	if *socket == "" || fs.NArg() != 0 {
		fmt.Fprintln(os.Stderr, "usage: textshot-serve -socket PATH [-idle DURATION] [textshot engine flags]")
		os.Exit(2)
	}
	level := slog.LevelInfo
	if *verbose {
		level = slog.LevelDebug
	}
	logger := slog.New(slog.NewTextHandler(os.Stderr, &slog.HandlerOptions{Level: level}))
	opts := textshot.Options{
		CacheDir: *cache, LibraryPath: *lib, Threads: *threads, Logger: logger,
		MaxSideLen: *maxSide, MinScore: float32(*minScore),
		ElementMaxSideLen: *elemMaxSide, ElementMinScore: float32(*elemMinScore),
	}
	if err := serve(*socket, *idle, opts, logger); err != nil {
		fmt.Fprintln(os.Stderr, "textshot-serve:", err)
		os.Exit(1)
	}
}

func serve(path string, idle time.Duration, opts textshot.Options, logger *slog.Logger) error {
	// One server per socket: the lock is held for its life; a second one exits quietly.
	lock, err := os.OpenFile(path+".lock", os.O_CREATE|os.O_RDWR, 0o600)
	if err != nil {
		return err
	}
	defer lock.Close()
	if err := syscall.Flock(int(lock.Fd()), syscall.LOCK_EX|syscall.LOCK_NB); err != nil {
		logger.Info("another server owns the socket", "socket", path)
		return nil
	}

	ctx, stop := signal.NotifyContext(context.Background(), os.Interrupt, syscall.SIGTERM)
	defer stop()
	start := time.Now()
	eng, err := textshot.New(ctx, opts)
	if err != nil {
		return err
	}
	defer eng.Close()
	// Load the element model now (upstream opens it lazily), so the first frame is warm.
	warm := image.NewRGBA(image.Rect(0, 0, 320, 180))
	for i := range warm.Pix {
		warm.Pix[i] = 0xff
	}
	warm.Set(10, 10, color.Black)
	if _, err := eng.DetectScreen(ctx, warm); err != nil {
		return fmt.Errorf("warm-up: %w", err)
	}
	logger.Info("ready", "socket", path, "seconds", time.Since(start).Seconds(), "textshot", version())

	// Clients connect only to a ready server: bind beside the path, then rename into place.
	_ = os.Remove(path)
	part := path + ".part"
	_ = os.Remove(part)
	ln, err := net.Listen("unix", part)
	if err != nil {
		return err
	}
	ln.(*net.UnixListener).SetUnlinkOnClose(false)
	if err := os.Rename(part, path); err != nil {
		ln.Close()
		return err
	}
	defer os.Remove(path)

	var mu sync.Mutex // one reading at a time: one engine
	last := make(chan struct{}, 1)
	go func() {
		for {
			conn, err := ln.Accept()
			if err != nil {
				return
			}
			go handle(ctx, conn, eng, &mu, last, logger)
		}
	}()
	timer := time.NewTimer(idle)
	defer timer.Stop()
	for {
		select {
		case <-ctx.Done():
			ln.Close()
			return nil
		case <-last:
			if !timer.Stop() {
				select {
				case <-timer.C:
				default:
				}
			}
			timer.Reset(idle)
		case <-timer.C:
			logger.Info("idle, exiting", "idle", idle)
			ln.Close()
			return nil
		}
	}
}

type request struct {
	Image string `json:"image"`
	Ping  bool   `json:"ping"`
}

func handle(ctx context.Context, conn net.Conn, eng *textshot.Engine, mu *sync.Mutex, last chan<- struct{}, logger *slog.Logger) {
	defer conn.Close()
	in := bufio.NewScanner(conn)
	in.Buffer(make([]byte, 64*1024), 1<<20)
	enc := json.NewEncoder(conn)
	for in.Scan() {
		select {
		case last <- struct{}{}:
		default:
		}
		var req request
		var reply any
		if err := json.Unmarshal(in.Bytes(), &req); err != nil {
			reply = map[string]string{"error": "bad request: " + err.Error()}
		} else if req.Ping {
			reply = map[string]any{"ok": true, "textshot": version()}
		} else {
			mu.Lock()
			elements, err := detect(ctx, eng, req.Image)
			mu.Unlock()
			if err != nil {
				reply = map[string]string{"error": err.Error()}
			} else {
				reply = map[string]any{"elements": elements}
			}
		}
		if err := enc.Encode(reply); err != nil {
			logger.Debug("client gone", "err", err)
			return
		}
	}
}

// detect is cmd/textshot's -json path for one image: load, DetectScreen, jsonElement.
func detect(ctx context.Context, eng *textshot.Engine, name string) ([]jsonElement, error) {
	img, err := load(name)
	if err != nil {
		return nil, err
	}
	scr, err := eng.DetectScreen(ctx, img)
	if err != nil {
		return nil, fmt.Errorf("%s: %w", name, err)
	}
	elements := []jsonElement{}
	for _, e := range scr.Elements {
		elements = append(elements, jsonElement{File: name, Element: e})
	}
	return elements, nil
}

// version is the go-textshot module this server is built with.
func version() string {
	if info, ok := debug.ReadBuildInfo(); ok {
		for _, d := range info.Deps {
			if d.Path == "github.com/devcell-sh/go-textshot" {
				return d.Version
			}
		}
	}
	return "unknown"
}

// load, jsonElement and its MarshalJSON are cmd/textshot's (go-textshot v0.2.0,
// cmd/textshot/main.go), unchanged but for load's error, which is returned, not fatal.
func load(name string) (image.Image, error) {
	f, err := os.Open(name)
	if err != nil {
		return nil, err
	}
	defer f.Close()
	img, _, err := image.Decode(f)
	if err != nil {
		return nil, fmt.Errorf("%s: decode: %w", name, err)
	}
	return img, nil
}

// jsonElement is a textshot.Element tagged with the file it came from.
type jsonElement struct {
	File    string
	Element textshot.Element
}

func (j jsonElement) MarshalJSON() ([]byte, error) {
	data, err := json.Marshal(j.Element)
	if err != nil {
		return nil, err
	}
	return append([]byte(`{"file":`+strconv.Quote(j.File)+`,`), data[1:]...), nil
}

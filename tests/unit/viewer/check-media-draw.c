// SPDX-FileCopyrightText: Mariotaku <git@mariotaku.me> and the Beanjar contributors
// SPDX-License-Identifier: GPL-2.0-or-later
/* A stream's software picture after hardware ones: the texture a DRM frame left bound as
 * GL_TEXTURE_EXTERNAL_OES must not be reused as GL_TEXTURE_2D (GL_INVALID_OPERATION, which once
 * ended the viewer at the next scanout copy). Links the real media.c and draws into an FBO of a
 * surfaceless EGL GLES 2 context (Mesa's llvmpipe will do); skipped (77) without one. */
#include "../../../host/viewer/core/media.c"

#include <EGL/egl.h>
#include <assert.h>
#include <stdio.h>
#include <string.h>

#define W 16
#define H 16

static int context(void)
{
    PFNEGLGETPLATFORMDISPLAYEXTPROC get_display =
        (PFNEGLGETPLATFORMDISPLAYEXTPROC)eglGetProcAddress("eglGetPlatformDisplayEXT");
    if (!get_display)
        return 0;
    EGLDisplay d = get_display(EGL_PLATFORM_SURFACELESS_MESA, EGL_DEFAULT_DISPLAY, NULL);
    if (d == EGL_NO_DISPLAY || !eglInitialize(d, NULL, NULL) || !eglBindAPI(EGL_OPENGL_ES_API))
        return 0;
    static const EGLint ca[] = {EGL_CONTEXT_CLIENT_VERSION, 2, EGL_NONE};
    EGLContext c = eglCreateContext(d, EGL_NO_CONFIG_KHR, EGL_NO_CONTEXT, ca);
    return c != EGL_NO_CONTEXT && eglMakeCurrent(d, EGL_NO_SURFACE, EGL_NO_SURFACE, c);
}

int main(void)
{
    if (!context()) {
        puts("SKIP no surfaceless EGL GLES 2 context");
        return 77;
    }
    const char *ext = (const char *)glGetString(GL_EXTENSIONS);
    if (!ext || !strstr(ext, "GL_OES_EGL_image_external")) {
        puts("SKIP no GL_OES_EGL_image_external");
        return 77;
    }
    static struct media m;
    gl_init(&m);
    assert(m.yuv);
    struct stream *s = &m.streams[0];
    pthread_mutex_init(&s->lock, NULL);
    s->m = &m;
    s->image = EGL_NO_IMAGE_KHR;
    s->pts_first = s->pts_last = NONE;

    GLuint target, fbo;
    glGenTextures(1, &target);
    glBindTexture(GL_TEXTURE_2D, target);
    glTexImage2D(GL_TEXTURE_2D, 0, GL_RGBA, W, H, 0, GL_RGBA, GL_UNSIGNED_BYTE, NULL);
    glGenFramebuffers(1, &fbo);
    glBindFramebuffer(GL_FRAMEBUFFER, fbo);
    glFramebufferTexture2D(GL_FRAMEBUFFER, GL_COLOR_ATTACHMENT0, GL_TEXTURE_2D, target, 0);
    assert(glCheckFramebufferStatus(GL_FRAMEBUFFER) == GL_FRAMEBUFFER_COMPLETE);
    glViewport(0, 0, W, H);
    glClearColor(0, 0, 0, 1);
    glClear(GL_COLOR_BUFFER_BIT);

    /* What a hardware picture leaves: the stream's textures, the first bound as external. */
    glGenTextures(3, s->tex);
    glBindTexture(GL_TEXTURE_EXTERNAL_OES, s->tex[0]);
    s->tex_oes = 1;
    assert(glGetError() == GL_NO_ERROR);

    /* Then a software picture: white in limited range (Y 235, U = V = 128), 4:2:0. */
    static uint8_t y[W * H], u[W / 2 * H / 2], v[W / 2 * H / 2];
    memset(y, 235, sizeof y);
    memset(u, 128, sizeof u);
    memset(v, 128, sizeof v);
    struct received_frame f = {.format = HM_YUV420,
                               .width = W,
                               .height = H,
                               .linesize = {W, W / 2, W / 2},
                               .pts = NONE,
                               .data = {y, u, v}};
    s->current = &f;
    s->uploaded = 0;
    draw_stream(&m, s);
    GLenum e = glGetError();
    if (e != GL_NO_ERROR)
        fprintf(stderr, "GL error 0x%x drawing a software picture after a hardware one\n", e);
    assert(e == GL_NO_ERROR);
    assert(s->shown == 1 && !s->tex_oes);
    glBindFramebuffer(GL_FRAMEBUFFER, fbo);
    uint8_t px[4];
    glReadPixels(W / 2, H / 2, 1, 1, GL_RGBA, GL_UNSIGNED_BYTE, px);
    assert(px[0] > 240 && px[1] > 240 && px[2] > 240);
    puts("PASS software picture after a hardware one: drawn, no GL error");
    s->current = NULL;
    return 0;
}

// SPDX-FileCopyrightText: Mariotaku <git@mariotaku.me> and the Beanjar contributors
// SPDX-License-Identifier: GPL-2.0-or-later
/* Mali's compute half in the vendor's one GL library (gl_vendor_gl, with the soc table's
 * gl_vendor_mali_compute): its UMP and OpenCL functions as the vendor's libmali.so answers them
 * on a set without its GPU driver: /dev/ump does not open, and OpenCL has no platform. Mesa has
 * neither API, and the emulator loads no Mali or UMP module.
 *
 * Who needs them (W18H 05.50.70 and W18M 05.50.60, M16PP and M16PP lite): LG's HAL,
 * libhal_lg115x.so.2's VPQ_LoadHspSymbols, dlopens /usr/local/lib/libOpenCL.so with
 * RTLD_NOW | RTLD_GLOBAL (bsppart's wrapper, NEEDing libmali.so: this library) and then
 * libehsp.so.0 RTLD_LAZY, LG's GPU-compute library for the smart local dimming and colour
 * contrast threads (HAL_VPQ_LED_LDSetSmartRun, HAL_VPQ_SetDominantColorDetectRun), which imports
 * ump_* and cl* and resolves them from the global scope. Without them tvservice's first lazy
 * call, hspCreateContext's ump_open after a resume, ends the process in ld.so ("undefined
 * symbol: ump_open", exit status 127).
 *
 * The answers are libmali.so.0.1's (W18M's bsppart) with no /dev/ump: ump_open 1 (UMP_ERROR),
 * the handle makers 0 (UMP_INVALID_MEMORY_HANDLE), ump_secure_id_get 0 for no handle,
 * ump_map NULL. One difference: libmali's ump_map reads the handle and faults on a NULL one;
 * here it is NULL. OpenCL reports no platform (CL_PLATFORM_NOT_FOUND_KHR, 0 platforms), which
 * libehsp's kernel libraries take as their failure, and every other call an error. No C library:
 * the empty stand-in is linked without one. */
#include <stddef.h>
#include <stdint.h>

#define EXPORT __attribute__((visibility("default")))

typedef void *ump_handle;
#define UMP_OK    0
#define UMP_ERROR 1

EXPORT int ump_open(void)
{
    return UMP_ERROR;
}

EXPORT void ump_close(void)
{
}

EXPORT ump_handle ump_allocate_64(uint64_t size, uint32_t flags)
{
    (void)size;
    (void)flags;
    return NULL;
}

EXPORT ump_handle ump_allocate_64_with_client_pid(uint64_t size, uint32_t flags, int pid)
{
    (void)size;
    (void)flags;
    (void)pid;
    return NULL;
}

EXPORT ump_handle ump_import(int type, void *phandle, uint32_t flags)
{
    (void)type;
    (void)phandle;
    (void)flags;
    return NULL;
}

EXPORT ump_handle ump_from_secure_id(uint32_t id)
{
    (void)id;
    return NULL;
}

EXPORT ump_handle ump_handle_create_from_secure_id(uint32_t id)
{
    (void)id;
    return NULL;
}

EXPORT uint32_t ump_secure_id_get(ump_handle h)
{
    (void)h;
    return 0;
}

EXPORT void *ump_map(ump_handle h, uint64_t offset, size_t size)
{
    (void)h;
    (void)offset;
    (void)size;
    return NULL;
}

EXPORT void ump_unmap(ump_handle h, void *addr, size_t size)
{
    (void)h;
    (void)addr;
    (void)size;
}

EXPORT void ump_release(ump_handle h)
{
    (void)h;
}

EXPORT void ump_retain(ump_handle h)
{
    (void)h;
}

EXPORT uint64_t ump_size_get_64(ump_handle h)
{
    (void)h;
    return 0;
}

EXPORT unsigned long ump_size_get(ump_handle h)
{
    (void)h;
    return 0;
}

/* ---- OpenCL: no platform ---- */

typedef int32_t cl_int;
typedef uint32_t cl_uint;
#define CL_INVALID_VALUE          (-30)
#define CL_INVALID_PLATFORM       (-32)
#define CL_INVALID_CONTEXT        (-34)
#define CL_INVALID_COMMAND_QUEUE  (-36)
#define CL_INVALID_MEM_OBJECT     (-38)
#define CL_INVALID_PROGRAM        (-44)
#define CL_INVALID_KERNEL         (-48)
#define CL_PLATFORM_NOT_FOUND_KHR (-1001)

static void *fail(cl_int *err, cl_int code)
{
    if (err)
        *err = code;
    return NULL;
}

EXPORT cl_int clGetPlatformIDs(cl_uint n, void **platforms, cl_uint *num)
{
    if ((!n && platforms) || (!platforms && !num))
        return CL_INVALID_VALUE;
    if (num)
        *num = 0;
    return CL_PLATFORM_NOT_FOUND_KHR;
}

EXPORT cl_int clGetDeviceIDs(void *platform, uint64_t type, cl_uint n, void **devices, cl_uint *num)
{
    (void)platform;
    (void)type;
    (void)n;
    (void)devices;
    if (num)
        *num = 0;
    return CL_INVALID_PLATFORM;
}

EXPORT cl_int clGetDeviceInfo(void *device, cl_uint name, size_t size, void *value, size_t *ret)
{
    (void)device;
    (void)name;
    (void)size;
    (void)value;
    (void)ret;
    return CL_INVALID_VALUE;
}

EXPORT void *clCreateContext(const void *props, cl_uint n, void *const *devices, void *notify,
                             void *user, cl_int *err)
{
    (void)props;
    (void)n;
    (void)devices;
    (void)notify;
    (void)user;
    return fail(err, CL_INVALID_PLATFORM);
}

EXPORT void *clCreateCommandQueue(void *ctx, void *device, uint64_t props, cl_int *err)
{
    (void)ctx;
    (void)device;
    (void)props;
    return fail(err, CL_INVALID_CONTEXT);
}

EXPORT void *clCreateProgramWithSource(void *ctx, cl_uint n, const char **src, const size_t *len,
                                       cl_int *err)
{
    (void)ctx;
    (void)n;
    (void)src;
    (void)len;
    return fail(err, CL_INVALID_CONTEXT);
}

EXPORT cl_int clBuildProgram(void *prog, cl_uint n, void *const *devices, const char *options,
                             void *notify, void *user)
{
    (void)prog;
    (void)n;
    (void)devices;
    (void)options;
    (void)notify;
    (void)user;
    return CL_INVALID_PROGRAM;
}

EXPORT cl_int clGetProgramBuildInfo(void *prog, void *device, cl_uint name, size_t size,
                                    void *value, size_t *ret)
{
    (void)prog;
    (void)device;
    (void)name;
    (void)size;
    (void)value;
    (void)ret;
    return CL_INVALID_PROGRAM;
}

EXPORT void *clCreateKernel(void *prog, const char *name, cl_int *err)
{
    (void)prog;
    (void)name;
    return fail(err, CL_INVALID_PROGRAM);
}

EXPORT cl_int clGetKernelWorkGroupInfo(void *kernel, void *device, cl_uint name, size_t size,
                                       void *value, size_t *ret)
{
    (void)kernel;
    (void)device;
    (void)name;
    (void)size;
    (void)value;
    (void)ret;
    return CL_INVALID_KERNEL;
}

EXPORT cl_int clSetKernelArg(void *kernel, cl_uint index, size_t size, const void *value)
{
    (void)kernel;
    (void)index;
    (void)size;
    (void)value;
    return CL_INVALID_KERNEL;
}

EXPORT cl_int clEnqueueNDRangeKernel(void *queue, void *kernel, cl_uint dims, const size_t *offset,
                                     const size_t *global, const size_t *local, cl_uint nwait,
                                     void *const *wait, void **event)
{
    (void)queue;
    (void)kernel;
    (void)dims;
    (void)offset;
    (void)global;
    (void)local;
    (void)nwait;
    (void)wait;
    (void)event;
    return CL_INVALID_COMMAND_QUEUE;
}

EXPORT cl_int clFinish(void *queue)
{
    (void)queue;
    return CL_INVALID_COMMAND_QUEUE;
}

EXPORT void *clCreateImage2D(void *ctx, uint64_t flags, const void *format, size_t w, size_t h,
                             size_t pitch, void *host, cl_int *err)
{
    (void)ctx;
    (void)flags;
    (void)format;
    (void)w;
    (void)h;
    (void)pitch;
    (void)host;
    return fail(err, CL_INVALID_CONTEXT);
}

/* cl_arm_import_memory */
EXPORT void *clImportMemoryARM(void *ctx, uint64_t flags, const void *props, void *memory,
                               size_t size, cl_int *err)
{
    (void)ctx;
    (void)flags;
    (void)props;
    (void)memory;
    (void)size;
    return fail(err, CL_INVALID_CONTEXT);
}

EXPORT cl_int clReleaseMemoryARM(void *mem)
{
    (void)mem;
    return CL_INVALID_MEM_OBJECT;
}

EXPORT cl_int clReleaseMemObject(void *mem)
{
    (void)mem;
    return CL_INVALID_MEM_OBJECT;
}

EXPORT cl_int clReleaseKernel(void *kernel)
{
    (void)kernel;
    return CL_INVALID_KERNEL;
}

EXPORT cl_int clReleaseProgram(void *prog)
{
    (void)prog;
    return CL_INVALID_PROGRAM;
}

EXPORT cl_int clReleaseCommandQueue(void *queue)
{
    (void)queue;
    return CL_INVALID_COMMAND_QUEUE;
}

EXPORT cl_int clReleaseContext(void *ctx)
{
    (void)ctx;
    return CL_INVALID_CONTEXT;
}

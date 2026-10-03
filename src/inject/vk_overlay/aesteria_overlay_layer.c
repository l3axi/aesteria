/* aesteria_overlay_layer.c — AESTERIA in-game overlay as a Vulkan implicit layer.
 *
 * WHY A VULKAN LAYER, FOR AION 2 SPECIFICALLY
 *   AION 2 is D3D12-only (Agility SDK, amd_fidelityfx_*_dx12.dll; there is no Vulkan
 *   renderer to hook). On Linux that means vkd3d-proton translates D3D12 -> Vulkan, so a
 *   Vulkan implicit layer still sits UNDERNEATH the game and sees the real
 *   vkQueuePresentKHR. Do not design a D3D12 hook: the layer gets the same frame with far
 *   less exposure.
 *
 * WHY THIS IS THE LOW-EXPOSURE FORM OF "INJECTION"
 *   The layer is a LINUX .so, loaded by the LINUX Vulkan loader, below the wine boundary.
 *   NCGuard (bb64.dll) is user-mode and enumerates the WINDOWS PEB module list; this object
 *   never appears there, for the same reason /proc/<pid>/mem reads do not. That is an
 *   argument about mechanism, NOT a measurement — the measurement is the game reaching
 *   in-world with the layer loaded, which is what this increment exists to establish.
 *
 * PROVENANCE: built on a Vulkan overlay layer that ran for months in other games. Everything
 *   marked "INHERITED FIX" below cost a killed session once; they are carried over verbatim
 *   in behaviour. ImGui, the IPC channels and pixtap are deliberately NOT included yet — this
 *   increment proves the present path and nothing else, so that a failure has exactly one
 *   possible cause.
 *
 * INCREMENT 1 (this file): negotiate, dispatch, swapchain track, intercept
 *   vkQueuePresentKHR, and paint a MOVING heartbeat block via vkCmdClearAttachments.
 *   Moving, not static, on purpose: a static box cannot tell "drawn every frame" from
 *   "drawn once into a frozen image".
 * INCREMENT 2 (next): Dear ImGui + the state/input/command IPC, state/input/command IPC to follow.
 *
 * ENABLE:  AESTERIA_OVERLAY=1, with the manifest in a Vulkan implicit_layer.d dir.
 * SMOKE:   AESTERIA_OVERLAY=1 AESTERIA_OVERLAY_ANYPROC=1 vkcube   (positive control FIRST)
 * BUILD:   needs vulkan-headers.  make
 */

#include <stdarg.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <strings.h>
#include <pthread.h>

#include <time.h>
#include <unistd.h>

#include <vulkan/vulkan.h>
#include <vulkan/vk_layer.h>

#define AESTERIA_LAYER_NAME "VK_LAYER_AESTERIA_overlay"
#define VK_LAYER_EXPORT __attribute__((visibility("default")))
#define MAX_IMAGES 8

static void logf_(const char *fmt, ...) {
    va_list ap;
    fprintf(stderr, "[aesteria-overlay] ");
    va_start(ap, fmt); vfprintf(stderr, fmt, ap); va_end(ap);
    fputc('\n', stderr);
    FILE *f = fopen("/tmp/aesteria-overlay.log", "a");
    if (f) {
        fprintf(f, "[aesteria-overlay] ");
        va_start(ap, fmt); vfprintf(f, fmt, ap); va_end(ap);
        fputc('\n', f);
        fclose(f);
    }
}

#define DISPATCH_KEY(obj) (*(void **)(obj))
#define VKOK(x) ((x) == VK_SUCCESS)

/* ── dispatch tables ── */
typedef struct InstanceData {
    void *key; VkInstance instance;
    PFN_vkGetInstanceProcAddr gipa;
    PFN_vkDestroyInstance DestroyInstance;
    struct InstanceData *next;
} InstanceData;

typedef struct SwapchainInfo {
    VkSwapchainKHR handle;
    VkExtent2D extent; VkFormat format; uint32_t image_count;
    int overlay_ok;                 /* 0 = disabled -> plain present */
    VkImage       images[MAX_IMAGES];
    VkImageView   views[MAX_IMAGES];
    VkFramebuffer fbs[MAX_IMAGES];
    VkCommandBuffer cmds[MAX_IMAGES];
    VkFence       fences[MAX_IMAGES];
    VkSemaphore   sems[MAX_IMAGES];
    VkRenderPass  render_pass;
    VkCommandPool cmd_pool;
    struct SwapchainInfo *next;
} SwapchainInfo;

typedef struct DeviceData {
    void *key; VkDevice device;
    uint32_t queue_family; VkQueue queue;
    unsigned long present_count;
    SwapchainInfo *swapchains;
    PFN_vkGetDeviceProcAddr gdpa;
    PFN_vkDestroyDevice DestroyDevice;
    PFN_vkGetDeviceQueue GetDeviceQueue;
    PFN_vkCreateSwapchainKHR CreateSwapchainKHR;
    PFN_vkDestroySwapchainKHR DestroySwapchainKHR;
    PFN_vkGetSwapchainImagesKHR GetSwapchainImagesKHR;
    PFN_vkQueuePresentKHR QueuePresentKHR;
    PFN_vkCreateRenderPass CreateRenderPass;
    PFN_vkDestroyRenderPass DestroyRenderPass;
    PFN_vkCreateImageView CreateImageView;
    PFN_vkDestroyImageView DestroyImageView;
    PFN_vkCreateFramebuffer CreateFramebuffer;
    PFN_vkDestroyFramebuffer DestroyFramebuffer;
    PFN_vkCreateCommandPool CreateCommandPool;
    PFN_vkDestroyCommandPool DestroyCommandPool;
    PFN_vkAllocateCommandBuffers AllocateCommandBuffers;
    PFN_vkBeginCommandBuffer BeginCommandBuffer;
    PFN_vkEndCommandBuffer EndCommandBuffer;
    PFN_vkResetCommandBuffer ResetCommandBuffer;
    PFN_vkCmdBeginRenderPass CmdBeginRenderPass;
    PFN_vkCmdEndRenderPass CmdEndRenderPass;
    PFN_vkCmdClearAttachments CmdClearAttachments;
    PFN_vkQueueSubmit QueueSubmit;
    PFN_vkCreateSemaphore CreateSemaphore;
    PFN_vkDestroySemaphore DestroySemaphore;
    PFN_vkCreateFence CreateFence;
    PFN_vkDestroyFence DestroyFence;
    PFN_vkWaitForFences WaitForFences;
    PFN_vkResetFences ResetFences;
    PFN_vkDeviceWaitIdle DeviceWaitIdle;
    /* Kept for increment 2 (ImGui's Vulkan backend resolves its own functions through these,
     * and memory properties are instance-level). Unused by the heartbeat draw. */
    VkPhysicalDevice phys;
    VkInstance instance;
    PFN_vkGetInstanceProcAddr gipa;
    struct DeviceData *next;
} DeviceData;

static pthread_mutex_t g_lock = PTHREAD_MUTEX_INITIALIZER;
static InstanceData *g_instances = NULL;
static DeviceData   *g_devices   = NULL;

static InstanceData *inst_get(void *k){for(InstanceData*d=g_instances;d;d=d->next)if(d->key==k)return d;return NULL;}
static DeviceData   *dev_get(void *k){for(DeviceData*d=g_devices;d;d=d->next)if(d->key==k)return d;return NULL;}

static VkLayerInstanceCreateInfo *find_instance_link(const VkInstanceCreateInfo *ci){
    VkLayerInstanceCreateInfo *p=(VkLayerInstanceCreateInfo*)ci->pNext;
    while(p && !(p->sType==VK_STRUCTURE_TYPE_LOADER_INSTANCE_CREATE_INFO && p->function==VK_LAYER_LINK_INFO))
        p=(VkLayerInstanceCreateInfo*)p->pNext;
    return p;
}
static VkLayerDeviceCreateInfo *find_device_link(const VkDeviceCreateInfo *ci){
    VkLayerDeviceCreateInfo *p=(VkLayerDeviceCreateInfo*)ci->pNext;
    while(p && !(p->sType==VK_STRUCTURE_TYPE_LOADER_DEVICE_CREATE_INFO && p->function==VK_LAYER_LINK_INFO))
        p=(VkLayerDeviceCreateInfo*)p->pNext;
    return p;
}

static VKAPI_ATTR PFN_vkVoidFunction VKAPI_CALL aest_GetInstanceProcAddr(VkInstance,const char*);
static VKAPI_ATTR PFN_vkVoidFunction VKAPI_CALL aest_GetDeviceProcAddr(VkDevice,const char*);

/* ── Is THIS process the game itself? ──────────────────────────────────────────────────
 * An implicit layer loads into EVERY Vulkan app, so the check is not optional: an
 * earlier overlay once happily drew over a game launcher.
 *
 * NOTE, AND IT MATTERS HERE: substring-matching the whole cmdline is the obvious check.
 * That would misfire for AION 2, because the launch chain hands the game's full path to
 * several OTHER processes — `steam.exe /games/.../AION2.exe`, the proton python3, the
 * reaper — so their cmdlines all CONTAIN "AION2.exe" while not being the game. So match
 * argv[0]'s basename exactly instead. (EpicWebHelper is excluded either way: its cmdline
 * carries "AION2/1.0.21..." as a user-agent, never "AION2.exe".)
 *
 * Under Wine /proc/self/exe is the wine loader, so argv[0] is where the Windows binary
 * name lives. Cached: it cannot change for the life of the process. */
static int process_is_game(void){
    static int cached = -1;
    if(cached >= 0) return cached;
    const char *any = getenv("AESTERIA_OVERLAY_ANYPROC");
    if(any && any[0]=='1'){ cached = 1; logf_("ANYPROC set — overlay armed in this process"); return cached; }
    cached = 0;
    FILE *f = fopen("/proc/self/cmdline","rb");
    if(f){
        char buf[4096];
        size_t n = fread(buf,1,sizeof(buf)-1,f);
        fclose(f);
        if(n > 0){
            buf[n] = '\0';                       /* argv[0] is the first NUL-terminated token */
            const char *base = buf;
            for(const char *p=buf; *p; p++)
                if(*p=='\\' || *p=='/') base = p+1;   /* Windows or Unix separators */
            if(!strcasecmp(base, "AION2.exe")) cached = 1;
        }
    }
    return cached;
}

/* ── vkCreateInstance ── */
static VKAPI_ATTR VkResult VKAPI_CALL aest_CreateInstance(
    const VkInstanceCreateInfo *ci, const VkAllocationCallbacks *al, VkInstance *pInstance){
    VkLayerInstanceCreateInfo *link=find_instance_link(ci);
    if(!link||!link->u.pLayerInfo) return VK_ERROR_INITIALIZATION_FAILED;
    PFN_vkGetInstanceProcAddr next=link->u.pLayerInfo->pfnNextGetInstanceProcAddr;
    PFN_vkCreateInstance create=(PFN_vkCreateInstance)next(NULL,"vkCreateInstance");
    if(!create) return VK_ERROR_INITIALIZATION_FAILED;
    link->u.pLayerInfo=link->u.pLayerInfo->pNext;
    VkResult r=create(ci,al,pInstance); if(r!=VK_SUCCESS) return r;
    InstanceData *d=calloc(1,sizeof(*d));
    if(!d) return VK_SUCCESS;                    /* layer degrades to a pass-through */
    d->key=DISPATCH_KEY(*pInstance); d->instance=*pInstance; d->gipa=next;
    d->DestroyInstance=(PFN_vkDestroyInstance)next(*pInstance,"vkDestroyInstance");
    pthread_mutex_lock(&g_lock); d->next=g_instances; g_instances=d; pthread_mutex_unlock(&g_lock);
    return VK_SUCCESS;
}
static VKAPI_ATTR void VKAPI_CALL aest_DestroyInstance(VkInstance instance,const VkAllocationCallbacks *al){
    void *k=DISPATCH_KEY(instance);
    pthread_mutex_lock(&g_lock);
    InstanceData **pp=&g_instances,*d=NULL;
    while(*pp){if((*pp)->key==k){d=*pp;*pp=d->next;break;}pp=&(*pp)->next;}
    pthread_mutex_unlock(&g_lock);
    if(d){d->DestroyInstance(instance,al);free(d);}
}

/* ── vkCreateDevice: capture queue + every fn we need ── */
static VKAPI_ATTR VkResult VKAPI_CALL aest_CreateDevice(
    VkPhysicalDevice phys, const VkDeviceCreateInfo *ci, const VkAllocationCallbacks *al, VkDevice *pDevice){
    VkLayerDeviceCreateInfo *link=find_device_link(ci);
    if(!link||!link->u.pLayerInfo) return VK_ERROR_INITIALIZATION_FAILED;
    PFN_vkGetInstanceProcAddr gipa=link->u.pLayerInfo->pfnNextGetInstanceProcAddr;
    PFN_vkGetDeviceProcAddr   gdpa=link->u.pLayerInfo->pfnNextGetDeviceProcAddr;
    PFN_vkCreateDevice create=(PFN_vkCreateDevice)gipa(NULL,"vkCreateDevice");
    if(!create) return VK_ERROR_INITIALIZATION_FAILED;
    link->u.pLayerInfo=link->u.pLayerInfo->pNext;
    VkResult r=create(phys,ci,al,pDevice); if(r!=VK_SUCCESS) return r;

    DeviceData *d=calloc(1,sizeof(*d));
    if(!d) return VK_SUCCESS;
    d->key=DISPATCH_KEY(*pDevice); d->device=*pDevice; d->gdpa=gdpa;
    d->queue_family = (ci->queueCreateInfoCount>0)? ci->pQueueCreateInfos[0].queueFamilyIndex : 0;
    d->phys=phys; d->gipa=gipa;
    d->instance = g_instances ? g_instances->instance : VK_NULL_HANDLE;
    #define GD(n) d->n=(PFN_vk##n)gdpa(*pDevice,"vk" #n)
    GD(DestroyDevice); GD(GetDeviceQueue); GD(CreateSwapchainKHR); GD(DestroySwapchainKHR);
    GD(GetSwapchainImagesKHR); GD(QueuePresentKHR); GD(CreateRenderPass); GD(DestroyRenderPass);
    GD(CreateImageView); GD(DestroyImageView); GD(CreateFramebuffer); GD(DestroyFramebuffer);
    GD(CreateCommandPool); GD(DestroyCommandPool); GD(AllocateCommandBuffers); GD(BeginCommandBuffer);
    GD(EndCommandBuffer); GD(ResetCommandBuffer); GD(CmdBeginRenderPass); GD(CmdEndRenderPass);
    GD(CmdClearAttachments); GD(QueueSubmit); GD(CreateSemaphore); GD(DestroySemaphore);
    GD(CreateFence); GD(DestroyFence); GD(WaitForFences); GD(ResetFences); GD(DeviceWaitIdle);
    #undef GD
    if(d->GetDeviceQueue) d->GetDeviceQueue(*pDevice,d->queue_family,0,&d->queue);
    pthread_mutex_lock(&g_lock); d->next=g_devices; g_devices=d; pthread_mutex_unlock(&g_lock);
    if(process_is_game()) logf_("device created (queue_family=%u) — heartbeat draw armed", d->queue_family);
    return VK_SUCCESS;
}

/* ── overlay resources for a swapchain (defensive: ANY failure -> overlay_ok=0) ──
 * The discipline here is the reason the overlay never took a session down by
 * a resource failure: no Vulkan result is assumed, and the degraded state is always
 * "no overlay this frame", never a crashed game. */
static void swapchain_teardown(DeviceData *d, SwapchainInfo *s){
    if(d->DeviceWaitIdle) d->DeviceWaitIdle(d->device);
    for(uint32_t i=0;i<s->image_count;i++){
        if(s->fences[i]) d->DestroyFence(d->device,s->fences[i],NULL);
        if(s->sems[i])   d->DestroySemaphore(d->device,s->sems[i],NULL);
        if(s->fbs[i])    d->DestroyFramebuffer(d->device,s->fbs[i],NULL);
        if(s->views[i])  d->DestroyImageView(d->device,s->views[i],NULL);
        s->fences[i]=VK_NULL_HANDLE; s->sems[i]=VK_NULL_HANDLE;
        s->fbs[i]=VK_NULL_HANDLE;    s->views[i]=VK_NULL_HANDLE;
    }
    if(s->cmd_pool)    d->DestroyCommandPool(d->device,s->cmd_pool,NULL);
    if(s->render_pass) d->DestroyRenderPass(d->device,s->render_pass,NULL);
    memset(&s->images,0,sizeof(s->images));
    s->cmd_pool=VK_NULL_HANDLE; s->render_pass=VK_NULL_HANDLE; s->overlay_ok=0;
}

static void swapchain_build(DeviceData *d, SwapchainInfo *s){
    s->overlay_ok=0;
    if(s->image_count==0 || s->image_count>MAX_IMAGES) return;
    if(!d->queue) return;
    if(d->GetSwapchainImagesKHR(d->device,s->handle,&s->image_count,s->images)!=VK_SUCCESS) return;

    /* LOAD/STORE with PRESENT_SRC on both ends: we paint ON TOP of the finished frame and
     * hand it back in the layout the app already promised the presentation engine. */
    VkAttachmentDescription att={ .format=s->format, .samples=VK_SAMPLE_COUNT_1_BIT,
        .loadOp=VK_ATTACHMENT_LOAD_OP_LOAD, .storeOp=VK_ATTACHMENT_STORE_OP_STORE,
        .stencilLoadOp=VK_ATTACHMENT_LOAD_OP_DONT_CARE, .stencilStoreOp=VK_ATTACHMENT_STORE_OP_DONT_CARE,
        .initialLayout=VK_IMAGE_LAYOUT_PRESENT_SRC_KHR, .finalLayout=VK_IMAGE_LAYOUT_PRESENT_SRC_KHR };
    VkAttachmentReference ref={0,VK_IMAGE_LAYOUT_COLOR_ATTACHMENT_OPTIMAL};
    VkSubpassDescription sub={ .pipelineBindPoint=VK_PIPELINE_BIND_POINT_GRAPHICS,
        .colorAttachmentCount=1, .pColorAttachments=&ref };
    VkRenderPassCreateInfo rpci={ .sType=VK_STRUCTURE_TYPE_RENDER_PASS_CREATE_INFO,
        .attachmentCount=1, .pAttachments=&att, .subpassCount=1, .pSubpasses=&sub };
    if(!VKOK(d->CreateRenderPass(d->device,&rpci,NULL,&s->render_pass))) return;

    VkCommandPoolCreateInfo pci={ .sType=VK_STRUCTURE_TYPE_COMMAND_POOL_CREATE_INFO,
        .flags=VK_COMMAND_POOL_CREATE_RESET_COMMAND_BUFFER_BIT, .queueFamilyIndex=d->queue_family };
    if(!VKOK(d->CreateCommandPool(d->device,&pci,NULL,&s->cmd_pool))){ swapchain_teardown(d,s); return; }
    VkCommandBufferAllocateInfo cbai={ .sType=VK_STRUCTURE_TYPE_COMMAND_BUFFER_ALLOCATE_INFO,
        .commandPool=s->cmd_pool, .level=VK_COMMAND_BUFFER_LEVEL_PRIMARY, .commandBufferCount=s->image_count };
    if(!VKOK(d->AllocateCommandBuffers(d->device,&cbai,s->cmds))){ swapchain_teardown(d,s); return; }

    for(uint32_t i=0;i<s->image_count;i++){
        VkImageViewCreateInfo iv={ .sType=VK_STRUCTURE_TYPE_IMAGE_VIEW_CREATE_INFO, .image=s->images[i],
            .viewType=VK_IMAGE_VIEW_TYPE_2D, .format=s->format,
            .subresourceRange={VK_IMAGE_ASPECT_COLOR_BIT,0,1,0,1} };
        if(!VKOK(d->CreateImageView(d->device,&iv,NULL,&s->views[i]))){ swapchain_teardown(d,s); return; }
        VkFramebufferCreateInfo fb={ .sType=VK_STRUCTURE_TYPE_FRAMEBUFFER_CREATE_INFO,
            .renderPass=s->render_pass, .attachmentCount=1, .pAttachments=&s->views[i],
            .width=s->extent.width, .height=s->extent.height, .layers=1 };
        if(!VKOK(d->CreateFramebuffer(d->device,&fb,NULL,&s->fbs[i]))){ swapchain_teardown(d,s); return; }
        /* Created SIGNALED so the very first bounded wait on this image passes. */
        VkFenceCreateInfo fc={ .sType=VK_STRUCTURE_TYPE_FENCE_CREATE_INFO, .flags=VK_FENCE_CREATE_SIGNALED_BIT };
        if(!VKOK(d->CreateFence(d->device,&fc,NULL,&s->fences[i]))){ swapchain_teardown(d,s); return; }
        VkSemaphoreCreateInfo sc={ .sType=VK_STRUCTURE_TYPE_SEMAPHORE_CREATE_INFO };
        if(!VKOK(d->CreateSemaphore(d->device,&sc,NULL,&s->sems[i]))){ swapchain_teardown(d,s); return; }
    }
    s->overlay_ok=1;
    logf_("overlay resources ready for %ux%u swapchain, %u images (draw ENABLED)",
          s->extent.width, s->extent.height, s->image_count);
}

/* ── vkCreateSwapchainKHR ── */
static VKAPI_ATTR VkResult VKAPI_CALL aest_CreateSwapchainKHR(
    VkDevice device, const VkSwapchainCreateInfoKHR *ci, const VkAllocationCallbacks *al, VkSwapchainKHR *pSC){
    DeviceData *d=dev_get(DISPATCH_KEY(device)); if(!d) return VK_ERROR_INITIALIZATION_FAILED;
    VkResult r=d->CreateSwapchainKHR(device,ci,al,pSC); if(r!=VK_SUCCESS) return r;
    uint32_t n=0; d->GetSwapchainImagesKHR(device,*pSC,&n,NULL);
    SwapchainInfo *s=calloc(1,sizeof(*s));
    if(!s) return VK_SUCCESS;
    s->handle=*pSC; s->extent=ci->imageExtent; s->format=ci->imageFormat; s->image_count=n;
    if(process_is_game()){
        logf_("swapchain created: %ux%u fmt=%d images=%u", s->extent.width,s->extent.height,(int)s->format,n);
        swapchain_build(d,s);
    }
    pthread_mutex_lock(&g_lock); s->next=d->swapchains; d->swapchains=s; pthread_mutex_unlock(&g_lock);
    return VK_SUCCESS;
}
static VKAPI_ATTR void VKAPI_CALL aest_DestroySwapchainKHR(
    VkDevice device, VkSwapchainKHR sc, const VkAllocationCallbacks *al){
    DeviceData *d=dev_get(DISPATCH_KEY(device));
    if(d){
        pthread_mutex_lock(&g_lock);
        SwapchainInfo **pp=&d->swapchains,*g=NULL;
        while(*pp){if((*pp)->handle==sc){g=*pp;*pp=g->next;break;}pp=&(*pp)->next;}
        pthread_mutex_unlock(&g_lock);
        if(g){ swapchain_teardown(d,g); free(g); }
        d->DestroySwapchainKHR(device,sc,al);
    }
}

/* ── the draw ────────────────────────────────────────────────────────────────────────
 * Increment 1: a heartbeat block that MOVES along the bottom edge, one step per present.
 * The movement is the whole point — a static rectangle proves the layer ran once, a moving
 * one proves it is in the live present path at frame rate. */
static VkResult overlay_draw_and_present(DeviceData *d, VkQueue queue, const VkPresentInfoKHR *pi){
    if(!process_is_game())        return d->QueuePresentKHR(queue,pi);
    if(pi->swapchainCount!=1)     return d->QueuePresentKHR(queue,pi);
    SwapchainInfo *s=NULL;
    for(SwapchainInfo *e=d->swapchains;e;e=e->next) if(e->handle==pi->pSwapchains[0]){s=e;break;}
    if(!s || !s->overlay_ok)      return d->QueuePresentKHR(queue,pi);

    uint32_t idx=pi->pImageIndices[0]; if(idx>=s->image_count) return d->QueuePresentKHR(queue,pi);
    VkCommandBuffer cb=s->cmds[idx];

    /* INHERITED FIX: BOUNDED wait, never UINT64_MAX. This fence is armed
     * by OUR QueueSubmit below, and the Begin/End/Submit failure paths return early — any one
     * of them leaves it UNSIGNALED, and an infinite wait the next time this image comes round
     * FROZE the game's entire presentation permanently (the game ran under the hood while the
     * present thread hung here forever). On timeout, present the app's own frame with no
     * overlay: a slow fence recovers next frame, a stuck one degrades to "overlay absent". */
    VkResult wr = d->WaitForFences(d->device,1,&s->fences[idx],VK_TRUE, 100000000ULL /* 100 ms */);
    if(wr != VK_SUCCESS) return d->QueuePresentKHR(queue,pi);
    d->ResetCommandBuffer(cb,0);

    VkCommandBufferBeginInfo bi={ .sType=VK_STRUCTURE_TYPE_COMMAND_BUFFER_BEGIN_INFO,
        .flags=VK_COMMAND_BUFFER_USAGE_ONE_TIME_SUBMIT_BIT };
    if(!VKOK(d->BeginCommandBuffer(cb,&bi))) return d->QueuePresentKHR(queue,pi);
    VkRenderPassBeginInfo rp={ .sType=VK_STRUCTURE_TYPE_RENDER_PASS_BEGIN_INFO,
        .renderPass=s->render_pass, .framebuffer=s->fbs[idx],
        .renderArea={{0,0},{s->extent.width,s->extent.height}} };
    d->CmdBeginRenderPass(cb,&rp,VK_SUBPASS_CONTENTS_INLINE);
    {
        const int bw=48, bh=16, margin=40;
        /* one step per present, wrapping across the usable width */
        int span = (int)s->extent.width - 2*margin - bw; if(span < 1) span = 1;
        int bx = margin + (int)((d->present_count * 6) % (unsigned long)span);
        int by = (int)s->extent.height - margin - bh; if(by < 0) by = 0;
        VkClearAttachment ca={ .aspectMask=VK_IMAGE_ASPECT_COLOR_BIT, .colorAttachment=0,
            .clearValue={ .color={ .float32={0.55f,0.30f,0.85f,0.90f} } } };   /* AESTERIA violet */
        VkClearRect cr={ .rect={{bx,by},{(uint32_t)bw,(uint32_t)bh}}, .baseArrayLayer=0, .layerCount=1 };
        d->CmdClearAttachments(cb,1,&ca,1,&cr);
    }
    d->CmdEndRenderPass(cb);
    if(!VKOK(d->EndCommandBuffer(cb))) return d->QueuePresentKHR(queue,pi);

    /* Our submit waits on the app's render-complete semaphores and signals ours; the present
     * then waits on ours instead. That is what orders our paint after the game's frame. */
    VkPipelineStageFlags stages[16];
    VkSubmitInfo si={ .sType=VK_STRUCTURE_TYPE_SUBMIT_INFO,
        .commandBufferCount=1, .pCommandBuffers=&cb,
        .signalSemaphoreCount=1, .pSignalSemaphores=&s->sems[idx] };
    if(pi->waitSemaphoreCount){
        uint32_t n=pi->waitSemaphoreCount>16?16:pi->waitSemaphoreCount;
        for(uint32_t i=0;i<n;i++) stages[i]=VK_PIPELINE_STAGE_COLOR_ATTACHMENT_OUTPUT_BIT;
        si.waitSemaphoreCount=n; si.pWaitSemaphores=pi->pWaitSemaphores; si.pWaitDstStageMask=stages;
    }
    /* INHERITED FIX: reset the fence ONLY here, immediately before re-arming it, so every
     * early return above leaves it SIGNALED from the previous completed submit. */
    d->ResetFences(d->device,1,&s->fences[idx]);
    if(!VKOK(d->QueueSubmit(queue,1,&si,s->fences[idx]))) return d->QueuePresentKHR(queue,pi);

    VkPresentInfoKHR mpi=*pi;
    mpi.waitSemaphoreCount=1; mpi.pWaitSemaphores=&s->sems[idx];
    return d->QueuePresentKHR(queue,&mpi);
}

static VKAPI_ATTR VkResult VKAPI_CALL aest_QueuePresentKHR(VkQueue queue, const VkPresentInfoKHR *pi){
    DeviceData *d=dev_get(DISPATCH_KEY(queue));
    if(!d){ for(DeviceData*e=g_devices;e;e=e->next) if(e->QueuePresentKHR) return e->QueuePresentKHR(queue,pi);
            return VK_ERROR_INITIALIZATION_FAILED; }
    unsigned long c=++d->present_count;
    if(c==1 && process_is_game()) logf_("first present intercepted — attempting overlay draw");
    if(process_is_game() && (c % 3600)==0) logf_("alive: %lu presents", c);
    return overlay_draw_and_present(d,queue,pi);
}

static VKAPI_ATTR void VKAPI_CALL aest_DestroyDevice(VkDevice device,const VkAllocationCallbacks *al){
    void *k=DISPATCH_KEY(device);
    pthread_mutex_lock(&g_lock);
    DeviceData **pp=&g_devices,*d=NULL;
    while(*pp){if((*pp)->key==k){d=*pp;*pp=d->next;break;}pp=&(*pp)->next;}
    pthread_mutex_unlock(&g_lock);
    if(d){
        if(d->DeviceWaitIdle) d->DeviceWaitIdle(d->device);   /* nothing in flight before teardown */
        for(SwapchainInfo *s=d->swapchains;s;){ SwapchainInfo *n=s->next; swapchain_teardown(d,s); free(s); s=n; }
        PFN_vkDestroyDevice dd=d->DestroyDevice; free(d); dd(device,al);
    }
}

/* ── GIPA / GDPA ── */
#define RET(name,fn) if(!strcmp(pName,name)) return (PFN_vkVoidFunction)(fn)
static VKAPI_ATTR PFN_vkVoidFunction VKAPI_CALL aest_GetDeviceProcAddr(VkDevice device,const char *pName){
    RET("vkGetDeviceProcAddr",aest_GetDeviceProcAddr);
    RET("vkDestroyDevice",aest_DestroyDevice);
    RET("vkCreateSwapchainKHR",aest_CreateSwapchainKHR);
    RET("vkDestroySwapchainKHR",aest_DestroySwapchainKHR);
    RET("vkQueuePresentKHR",aest_QueuePresentKHR);
    DeviceData *d=dev_get(DISPATCH_KEY(device));
    if(!d||!d->gdpa) return NULL;
    return d->gdpa(device,pName);
}
static VKAPI_ATTR PFN_vkVoidFunction VKAPI_CALL aest_GetInstanceProcAddr(VkInstance instance,const char *pName){
    RET("vkGetInstanceProcAddr",aest_GetInstanceProcAddr);
    RET("vkCreateInstance",aest_CreateInstance);
    RET("vkDestroyInstance",aest_DestroyInstance);
    RET("vkCreateDevice",aest_CreateDevice);
    RET("vkGetDeviceProcAddr",aest_GetDeviceProcAddr);
    RET("vkDestroyDevice",aest_DestroyDevice);
    RET("vkCreateSwapchainKHR",aest_CreateSwapchainKHR);
    RET("vkDestroySwapchainKHR",aest_DestroySwapchainKHR);
    RET("vkQueuePresentKHR",aest_QueuePresentKHR);
    if(!instance) return NULL;
    InstanceData *d=inst_get(DISPATCH_KEY(instance));
    if(!d||!d->gipa) return NULL;
    return d->gipa(instance,pName);
}

VK_LAYER_EXPORT VKAPI_ATTR VkResult VKAPI_CALL aesteria_vkNegotiateLoaderLayerInterfaceVersion(
    VkNegotiateLayerInterface *v){
    if(!v || v->sType!=LAYER_NEGOTIATE_INTERFACE_STRUCT) return VK_ERROR_INITIALIZATION_FAILED;
    if(v->loaderLayerInterfaceVersion>2) v->loaderLayerInterfaceVersion=2;
    v->pfnGetInstanceProcAddr=aest_GetInstanceProcAddr;
    v->pfnGetDeviceProcAddr=aest_GetDeviceProcAddr;
    v->pfnGetPhysicalDeviceProcAddr=NULL;
    if(process_is_game()) logf_("negotiated loader interface v%u (increment 1)", v->loaderLayerInterfaceVersion);
    return VK_SUCCESS;
}
